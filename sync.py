#!/usr/bin/env python3
"""Fast-forward forks to the newest upstream commit whose CI is green.

For each repo, look at the upstream branch's first-parent history (commits the branch
itself pointed at, not commits from merged PR branches) and pick the newest commit that:
  - is at least --min-age minutes old, so every workflow has had time to start,
  - has at least one check run or commit status,
  - has every check run finished as success, skipped or neutral (nothing failed,
    cancelled, timed out or still running), ignoring names matched by --ignore-check and
    runs of workflows started by an --ignore-event (for example nightly `schedule` runs),
  - has every workflow run for it finished the same way, even one cancelled or queued before
    any job reported (same exceptions; --ignore-check also matches workflow names),
  - has a combined commit status of success, if it has any statuses.
The fork's branch is then fast-forwarded to it. With --docs-path, newer commits that have no
checks and only change matching files (README, .coderabbit.yaml, ...) are taken along too. Nothing is ever force-pushed: a fork with
commits of its own is reported and left alone.

API calls go through the `gh` CLI (GH_TOKEN in Actions, the local gh login otherwise).
Standard library only.
"""
import argparse
import base64
import datetime as dt
import fnmatch
import json
import os
import subprocess
import sys
import tempfile

GOOD = {"success", "skipped", "neutral"}


def api(path, method="GET", fields=None, ok_codes=False):
    cmd = ["gh", "api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    for k, v in (fields or {}).items():
        if isinstance(v, bool):
            cmd += ["-F", f"{k}={str(v).lower()}"]
        else:
            cmd += ["-f", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if ok_codes:
            return None, (r.stdout + r.stderr).strip()
        raise RuntimeError(f"{method} {path}: {(r.stdout + r.stderr).strip()[:300]}")
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    return (out, None) if ok_codes else out


class Syncer:
    def __init__(self, cfg):
        self.c = cfg

    def green(self, repo, sha):
        """Return (is_green, reason) for an upstream commit."""
        up = self.c.upstream

        def ignored(name):
            return any(fnmatch.fnmatch(name, p) for p in self.c.ignore_checks)

        def pages(path, key):  # either list can pass 100 on a big repo
            out, page = [], 1
            while True:
                got = api(f"{path}per_page=100&page={page}")[key]
                out += got
                if len(got) < 100:
                    return out
                page += 1
        every = pages(f"repos/{up}/{repo}/commits/{sha}/check-runs?filter=latest&", "check_runs")
        runs = [c for c in every if not ignored(c["name"])]
        # Workflow runs too: one cancelled or still queued before any job started leaves no
        # check runs behind, so the commit's other checks alone would look green.
        workflows = pages(f"repos/{up}/{repo}/actions/runs?head_sha={sha}&", "workflow_runs")
        # A nightly workflow on the branch tip adds its check runs to a commit that was
        # already green on push; leave runs from the ignored events out.
        skip = {w["check_suite_id"] for w in workflows if w["event"] in self.c.ignore_events}
        runs = [c for c in runs if c.get("check_suite", {}).get("id") not in skip]
        latest = {}
        for w in workflows:
            if w["event"] not in self.c.ignore_events and not ignored(w["name"]):
                if w["workflow_id"] not in latest or w["id"] > latest[w["workflow_id"]]["id"]:
                    latest[w["workflow_id"]] = w
        wf_running, wf_bad = [], []
        for w in latest.values():
            name = w["name"] if len(w["name"]) <= 40 else w["name"][:39] + "…"  # Dependabot's are long
            # A workflow kept open, or failed, only by an ignored job doesn't count.
            mine = [c for c in every if c.get("check_suite", {}).get("id") == w["check_suite_id"] and ignored(c["name"])]
            if w["status"] != "completed":
                if not any(c["status"] != "completed" for c in mine):
                    wf_running.append(name)
            elif w["conclusion"] not in GOOD and not any(c["conclusion"] not in GOOD for c in mine):
                wf_bad.append(f'{name}={w["conclusion"]}')
        if wf_running:
            return False, "workflow running: " + ", ".join(sorted(wf_running)[:3])
        if wf_bad:
            return False, "workflow not green: " + ", ".join(sorted(wf_bad)[:3])
        status = api(f"repos/{up}/{repo}/commits/{sha}/status")
        if not runs and status["total_count"] == 0:
            return False, "no checks"
        names = {c["name"] for c in runs}
        missing = [p for p in self.c.require_checks if not any(fnmatch.fnmatch(n, p) for n in names)]
        if missing:
            return False, "missing required: " + ", ".join(missing[:3])
        pending = [c["name"] for c in runs if c["status"] != "completed"]
        if pending:
            return False, f"{len(pending)} running"
        bad = [f'{c["name"]}={c["conclusion"]}' for c in runs if c["conclusion"] not in GOOD]
        if bad:
            return False, "not green: " + ", ".join(bad[:3])
        if status["total_count"] and status["state"] != "success":
            return False, f"status {status['state']}"
        return True, f"{len(runs)} checks green"

    def docs_only(self, repo, commit):
        """True if the commit (against its first parent) only changes files matching --docs-path."""
        if not commit["parents"]:
            return False
        cmp = api(f"repos/{self.c.upstream}/{repo}/compare/{commit['parents'][0]['sha']}...{commit['sha']}")
        files = cmp.get("files") or []
        if not files or len(files) >= 300:  # compare lists at most 300 files
            return False
        paths = [p for f in files for p in (f["filename"], f.get("previous_filename")) if p]
        return all(any(fnmatch.fnmatch(p, pat) for pat in self.c.docs_paths) for p in paths)

    def push_with_git(self, repo, sha):
        """Fallback when the commit isn't in the fork's object store yet (repos that GitHub
        doesn't link as a fork). Fetch from upstream, push without force."""
        token = os.environ.get("GH_TOKEN") or subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        with tempfile.TemporaryDirectory() as d:
            def git(*a):
                subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True)
            git("init", "-q", "--bare")
            git("fetch", "-q", f"https://github.com/{self.c.upstream}/{repo}.git", sha)
            git("-c", f"http.extraheader=AUTHORIZATION: basic {auth}", "push", "-q",
                f"https://github.com/{self.c.fork}/{repo}.git", f"{sha}:refs/heads/{self.c.fork_branch}")

    def move(self, repo, fork_sha, sha, ahead, why):
        c = self.c
        if c.dry_run:
            return f"would move {ahead} commits to {sha[:8]} ({why})", fork_sha, sha
        _, err = api(f"repos/{c.fork}/{repo}/git/refs/heads/{c.fork_branch}", "PATCH",
                     {"sha": sha, "force": False}, ok_codes=True)
        if err:
            if "Object does not exist" not in err and "422" not in err:
                raise RuntimeError(err[:300])
            self.push_with_git(repo, sha)
        return f"moved {ahead} commits to {sha[:8]} ({why})", fork_sha, sha

    def sync(self, repo):
        c = self.c
        fork_sha = api(f"repos/{c.fork}/{repo}/commits/{c.fork_branch}")["sha"]
        listed = api(f"repos/{c.upstream}/{repo}/commits?sha={c.branch}&per_page={c.lookback}")
        if listed[0]["sha"] == fork_sha:
            return "up to date", fork_sha, None
        # First-parent walk from the tip: only commits the upstream branch itself pointed at.
        by_sha, chain, cur = {x["sha"]: x for x in listed}, [], listed[0]
        while cur:
            chain.append(cur)
            parents = cur["parents"]
            cur = by_sha.get(parents[0]["sha"]) if parents else None
        now = dt.datetime.now(dt.timezone.utc)
        notes = []
        docs_tip = None  # newest of the unchecked docs-only commits directly above the current one
        for commit in chain:
            sha = commit["sha"]
            if sha == fork_sha:
                if not docs_tip:
                    break
                # Only unchecked docs commits since the fork's commit: take them along.
                sha, why = docs_tip, f"docs only on top of {fork_sha[:8]}"
                cmp = api(f"repos/{c.upstream}/{repo}/compare/{fork_sha}...{sha}")
                return self.move(repo, fork_sha, sha, cmp["ahead_by"], why)
            when = dt.datetime.fromisoformat(commit["commit"]["committer"]["date"].replace("Z", "+00:00"))
            if now - when < dt.timedelta(minutes=c.min_age):
                notes.append(f"{sha[:8]} too new")
                docs_tip = None
                continue
            ok, why = self.green(repo, sha)
            if not ok:
                if why == "no checks" and c.docs_paths and self.docs_only(repo, commit):
                    docs_tip = docs_tip or sha
                    notes.append(f"{sha[:8]} docs only")
                else:
                    notes.append(f"{sha[:8]} {why}")
                    docs_tip = None
                continue
            if docs_tip:
                why = f"docs only on top of {sha[:8]}, {why}"
                sha = docs_tip
            cmp = api(f"repos/{c.upstream}/{repo}/compare/{fork_sha}...{sha}")
            if cmp["status"] != "ahead":
                tip = api(f"repos/{c.upstream}/{repo}/compare/{fork_sha}...{chain[0]['sha']}")
                if tip["status"] != "ahead":
                    return "fork has diverged from upstream, not touching it", fork_sha, None
                notes.append(f"{sha[:8]} not a fast-forward")
                continue
            return self.move(repo, fork_sha, sha, cmp["ahead_by"], why)
        return "waiting: no newer green commit (" + "; ".join(notes[:3]) + ")", fork_sha, None


def split_list(text):
    out = []
    for line in (text or "").replace(",", "\n").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("repos", nargs="*", help="repo names (default: --repos-file / SYNC_REPOS)")
    ap.add_argument("--upstream", default=env("SYNC_UPSTREAM"), help="upstream owner (user or org)")
    ap.add_argument("--fork", default=env("SYNC_FORK"), help="fork owner (user or org)")
    ap.add_argument("--branch", default=env("SYNC_BRANCH") or "main", help="upstream branch")
    ap.add_argument("--fork-branch", default=env("SYNC_FORK_BRANCH") or "", help="fork branch (default: --branch)")
    ap.add_argument("--repos-file", default=env("SYNC_REPOS_FILE") or "")
    ap.add_argument("--min-age", type=int, default=int(env("SYNC_MIN_AGE") or 10), help="minutes")
    ap.add_argument("--lookback", type=int, default=int(env("SYNC_LOOKBACK") or 40), help="upstream commits to scan (max 100)")
    ap.add_argument("--ignore-check", dest="ignore_checks", action="append", default=split_list(env("SYNC_IGNORE_CHECKS")),
                    help="check-run or workflow name pattern to ignore (repeatable, * wildcards)")
    ap.add_argument("--require-check", dest="require_checks", action="append", default=split_list(env("SYNC_REQUIRE_CHECKS")),
                    help="check-run name pattern that must be present (repeatable)")
    ap.add_argument("--ignore-event", dest="ignore_events", action="append", default=split_list(env("SYNC_IGNORE_EVENTS")),
                    help="leave out check runs of workflows started by this event, e.g. schedule (repeatable)")
    ap.add_argument("--docs-path", dest="docs_paths", action="append", default=split_list(env("SYNC_DOCS_PATHS")),
                    help="file pattern whose changes need no CI, e.g. *.md (repeatable)")
    ap.add_argument("--dry-run", action="store_true", default=(env("SYNC_DRY_RUN") or "").lower() == "true")
    ap.add_argument("--status", default=env("SYNC_STATUS_FILE") or "", help="write a Markdown status table here")
    c = ap.parse_args()
    if not c.upstream or not c.fork:
        ap.error("--upstream and --fork (or SYNC_UPSTREAM / SYNC_FORK) are required")
    c.fork_branch = c.fork_branch or c.branch
    c.lookback = max(1, min(c.lookback, 100))
    repos = c.repos or split_list(env("SYNC_REPOS"))
    if c.repos_file:
        with open(c.repos_file) as f:
            repos += split_list(f.read())
    if not repos:
        ap.error("no repos given (arguments, SYNC_REPOS or --repos-file)")

    s = Syncer(c)
    rows, failed, moved = [], 0, 0
    for repo in repos:
        try:
            result, before, after = s.sync(repo)
        except Exception as e:  # keep going with the other repos
            result, before, after, failed = f"ERROR {e}", "", None, failed + 1
        moved += result.startswith("moved")
        print(f"{repo:24} {result}", flush=True)
        rows.append((repo, (after or before)[:8], result))

    table = (f"| repo | {c.fork} {c.fork_branch} | last result |\n|---|---|---|\n" + "".join(
        f"| [{r}](https://github.com/{c.fork}/{r}) | `{sha}` | {res.split(' (')[0]} |\n" for r, sha, res in rows))
    if c.status:
        with open(c.status, "w") as f:
            f.write("# Fork sync status\n\n" + table)
    if env("GITHUB_STEP_SUMMARY"):
        with open(env("GITHUB_STEP_SUMMARY"), "a") as f:
            f.write(f"### Fork sync{' (dry run)' if c.dry_run else ''}\n\n" + table)
    if env("GITHUB_OUTPUT"):
        with open(env("GITHUB_OUTPUT"), "a") as f:
            f.write(f"moved={moved}\nfailed={failed}\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
