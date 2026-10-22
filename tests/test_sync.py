"""Offline tests for sync.py: GitHub's API is replaced by an in-memory fake."""
import argparse
import datetime as dt
import os
import re
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import sync  # noqa: E402

OLD = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
NEW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(name):
    with open(os.path.join(ROOT, name)) as f:
        return f.read()


def run(name, conclusion="success", status="completed", suite=1):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "check_suite": {"id": suite}}


def wf(name, conclusion="success", status="completed", suite=1, event="push", run_id=None, workflow=None):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None,
            "check_suite_id": suite, "event": event, "id": run_id or suite, "workflow_id": workflow or name}


class FakeGitHub:
    """Just enough of the REST API for one upstream repo and its fork."""

    def __init__(self, commits, fork_sha):
        # commits: list of (sha, [parent shas], date), newest first, as the commits API lists them
        self.commits = commits
        self.parents = {s: p for s, p, _ in commits}
        self.fork_sha = fork_sha
        self.checks = {}      # sha -> list of check runs
        self.statuses = {}    # sha -> (state, count)
        self.workflow_runs = {}  # sha -> list of (check suite id, event) or wf() dicts
        self.files = {}       # sha -> files the commit changes
        self.patches = []
        self.patch_error = None

    def ancestors(self, sha):
        seen, todo = set(), [sha]
        while todo:
            s = todo.pop()
            if s in seen:
                continue
            seen.add(s)
            todo += self.parents.get(s, [])
        return seen

    def __call__(self, path, method="GET", fields=None, ok_codes=False):
        def reply(x):
            return (x, None) if ok_codes else x
        if method == "PATCH":
            if self.patch_error:
                return None, self.patch_error
            self.patches.append(fields)
            self.fork_sha = fields["sha"]
            return reply({})
        if path == "repos/fork/r/commits/main":
            return reply({"sha": self.fork_sha})
        if re.fullmatch(r"repos/up/r/commits\?sha=main&per_page=\d+", path):
            return reply([{"sha": s, "parents": [{"sha": p} for p in ps], "commit": {"committer": {"date": d}}}
                          for s, ps, d in self.commits])
        if m := re.fullmatch(r"repos/up/r/commits/(\w+)/check-runs\?filter=latest&per_page=100&page=(\d+)", path):
            runs, page = self.checks.get(m[1], []), int(m[2])
            return reply({"check_runs": runs[(page - 1) * 100:page * 100]})
        if m := re.fullmatch(r"repos/up/r/actions/runs\?head_sha=(\w+)&per_page=100&page=(\d+)", path):
            runs = [w if isinstance(w, dict) else wf(f"wf{w[0]}", suite=w[0], event=w[1])
                    for w in self.workflow_runs.get(m[1], [])]
            page = int(m[2])
            return reply({"workflow_runs": runs[(page - 1) * 100:page * 100]})
        if m := re.fullmatch(r"repos/up/r/commits/(\w+)/status", path):
            state, count = self.statuses.get(m[1], ("pending", 0))
            return reply({"state": state, "total_count": count})
        if m := re.fullmatch(r"repos/up/r/compare/(\w+)\.\.\.(\w+)", path):
            base, head = m[1], m[2]
            if base == head:
                return reply({"status": "identical", "ahead_by": 0})
            if base in self.ancestors(head):
                new = self.ancestors(head) - self.ancestors(base)
                files = sorted({f for s in new for f in self.files.get(s, ["src/main.rs"])})
                return reply({"status": "ahead", "ahead_by": len(new), "files": [{"filename": f} for f in files]})
            return reply({"status": "diverged", "ahead_by": 0})
        raise AssertionError(f"unexpected API call: {method} {path}")


def cfg(**kw):
    base = dict(upstream="up", fork="fork", branch="main", fork_branch="main", min_age=10,
                lookback=40, ignore_checks=[], require_checks=[], ignore_events=[], docs_paths=[], dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class SyncTests(unittest.TestCase):
    def setUp(self):
        # main: c3 -> c2 -> c1 -> c0 (fork is at c0)
        self.gh = FakeGitHub([("c3", ["c2"], OLD), ("c2", ["c1"], OLD), ("c1", ["c0"], OLD), ("c0", [], OLD)], "c0")
        patcher = mock.patch.object(sync, "api", self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sync(self, **kw):
        return sync.Syncer(cfg(**kw)).sync("r")

    def test_up_to_date(self):
        self.gh.fork_sha = "c3"
        self.assertEqual(self.sync()[0], "up to date")
        self.assertEqual(self.gh.patches, [])

    def test_moves_to_newest_green_without_force(self):
        self.gh.checks = {"c3": [run("build"), run("lint", "skipped")], "c2": [run("build")]}
        result, before, after = self.sync()
        self.assertTrue(result.startswith("moved 3 commits to c3"), result)
        self.assertEqual((before, after), ("c0", "c3"))
        self.assertEqual(self.gh.patches, [{"sha": "c3", "force": False}])

    def test_cancelled_or_failed_falls_back_to_older_green(self):
        self.gh.checks = {"c3": [run("build", "cancelled")], "c2": [run("build", "failure")], "c1": [run("build")]}
        self.assertTrue(self.sync()[0].startswith("moved 1 commits to c1"))

    def test_running_checks_wait(self):
        self.gh.checks = {"c3": [run("build", status="in_progress")]}
        result, _, after = self.sync()
        self.assertIn("1 running", result)
        self.assertIsNone(after)
        self.assertEqual(self.gh.patches, [])

    def test_commit_without_checks_is_skipped(self):
        self.gh.checks = {"c2": [run("build")]}  # c3 has none
        self.assertTrue(self.sync()[0].startswith("moved 2 commits to c2"))

    def test_too_new_commit_is_skipped(self):
        self.gh.commits[0] = ("c3", ["c2"], NEW)
        self.gh.checks = {"c3": [run("build")], "c2": [run("build")]}
        self.assertTrue(self.sync()[0].startswith("moved 2 commits to c2"))

    def test_only_first_parent_commits_count(self):
        # c3 merges side-branch commit s1 (second parent); s1 is green but main never pointed at it
        self.gh = FakeGitHub([("c3", ["c2", "s1"], OLD), ("s1", ["c0"], OLD), ("c2", ["c1"], OLD),
                              ("c1", ["c0"], OLD), ("c0", [], OLD)], "c0")
        self.gh.checks = {"c3": [run("build", "failure")], "s1": [run("build")]}
        with mock.patch.object(sync, "api", self.gh):
            result, _, after = self.sync()
        self.assertTrue(result.startswith("waiting"), result)
        self.assertIsNone(after)

    def test_diverged_fork_is_left_alone(self):
        self.gh.fork_sha = "x9"  # a commit upstream doesn't have
        self.gh.checks = {"c3": [run("build")]}
        self.assertIn("diverged", self.sync()[0])
        self.assertEqual(self.gh.patches, [])

    def test_ignore_checks_pattern(self):
        self.gh.checks = {"c3": [run("build"), run("Dependabot", "failure"), run("freebsd / x86", "cancelled")]}
        self.assertTrue(self.sync()[0].startswith("waiting"))
        self.assertTrue(self.sync(ignore_checks=["Dependabot", "freebsd*"])[0].startswith("moved 3 commits to c3"))

    def test_only_ignored_checks_counts_as_no_checks(self):
        self.gh.checks = {"c3": [run("Dependabot")]}
        self.assertIn("no checks", self.sync(ignore_checks=["Dependabot"])[0])

    def test_ignore_events_drops_scheduled_runs(self):
        # c3 was green on push, then a nightly run on the branch tip failed / is still running
        self.gh.checks = {"c3": [run("build"), run("freebsd / x86", "failure", suite=2),
                                 run("freebsd / arm", status="in_progress", suite=2)]}
        self.gh.workflow_runs = {"c3": [(1, "push"), (2, "schedule")]}
        self.assertTrue(self.sync()[0].startswith("waiting"))
        self.assertTrue(self.sync(ignore_events=["schedule"])[0].startswith("moved 3 commits to c3"))

    def test_ignore_events_keeps_failing_push_runs(self):
        self.gh.checks = {"c3": [run("build", "failure"), run("nightly", suite=2)]}
        self.gh.workflow_runs = {"c3": [(1, "push"), (2, "schedule")]}
        self.assertTrue(self.sync(ignore_events=["schedule"])[0].startswith("waiting"))

    def test_cancelled_workflow_without_check_runs_is_not_green(self):
        # c3: CI was cancelled before any job reported; only another workflow's checks passed
        self.gh.checks = {"c3": [run("lint / lint", suite=2)], "c2": [run("build")]}
        self.gh.workflow_runs = {"c3": [wf("CI", "cancelled", suite=1), wf("Packaging lint", suite=2)],
                                 "c2": [wf("CI")]}
        result = self.sync()[0]
        self.assertTrue(result.startswith("moved 2 commits to c2"), result)
        self.gh.fork_sha = "c0"
        self.gh.checks.pop("c2")
        self.assertIn("c3 workflow not green: CI=cancelled", self.sync()[0])

    def test_queued_workflow_without_check_runs_waits(self):
        self.gh.checks = {"c3": [run("SonarCloud Scan", suite=2)]}
        self.gh.workflow_runs = {"c3": [wf("CI", status="pending", suite=1), wf("SonarCloud", suite=2)]}
        result, _, after = self.sync(ignore_checks=["*SonarCloud*"])
        self.assertIn("c3 workflow running: CI", result)
        self.assertIsNone(after)

    def test_cancelled_workflow_blocks_docs_only_commit(self):
        self.gh.checks = {"c2": [run("build")]}
        self.gh.files = {"c3": ["README.md"]}
        self.gh.workflow_runs = {"c3": [wf("CI", "cancelled")]}
        self.assertTrue(self.sync(docs_paths=["*.md"])[0].startswith("moved 2 commits to c2 ("))

    def test_ignored_workflows_and_events_do_not_block(self):
        self.gh.checks = {"c3": [run("build")]}
        self.gh.workflow_runs = {"c3": [wf("CI"), wf("FreeBSD", "cancelled", suite=2),
                                        wf("Nightly", status="in_progress", suite=3, event="schedule")]}
        self.assertTrue(self.sync()[0].startswith("waiting"))
        result = self.sync(ignore_checks=["*FreeBSD*"], ignore_events=["schedule"])[0]
        self.assertTrue(result.startswith("moved 3 commits to c3"), result)

    def test_workflow_held_up_only_by_ignored_job_does_not_block(self):
        # CI's FreeBSD job is ignored; it failed (or is still running) while the rest passed
        self.gh.checks = {"c3": [run("build"), run("freebsd / x86", "failure")],
                          "c2": [run("build"), run("freebsd / x86", status="in_progress")]}
        self.gh.workflow_runs = {"c3": [wf("CI", "failure")], "c2": [wf("CI", status="in_progress")]}
        self.assertTrue(self.sync()[0].startswith("waiting"))
        self.assertTrue(self.sync(ignore_checks=["freebsd*"])[0].startswith("moved 3 commits to c3"))
        self.gh.fork_sha = "c0"
        self.gh.workflow_runs["c3"] = [wf("CI", "cancelled")]  # cancelled, but not by the ignored job
        self.gh.checks["c3"] = [run("build"), run("freebsd / x86")]
        self.assertTrue(self.sync(ignore_checks=["freebsd*"])[0].startswith("moved 2 commits to c2"))

    def test_only_the_newest_run_of_a_workflow_counts(self):
        self.gh.checks = {"c3": [run("build", suite=2)]}
        self.gh.workflow_runs = {"c3": [wf("CI", "cancelled", suite=1, run_id=10),
                                        wf("CI", suite=2, run_id=11, event="workflow_dispatch")]}
        self.assertTrue(self.sync()[0].startswith("moved 3 commits to c3"))

    def test_workflow_runs_are_paginated(self):
        self.gh.checks = {"c3": [run("build")]}
        self.gh.workflow_runs = {"c3": [wf(f"w{i}", suite=i + 1) for i in range(100)] + [wf("late", "failure", suite=200)]}
        self.assertIn("late=failure", self.sync()[0])

    def test_docs_only_commits_on_top_of_green_are_taken(self):
        self.gh.checks = {"c1": [run("build")]}
        self.gh.files = {"c3": [".coderabbit.yaml"], "c2": ["README.md", "docs/a.md"]}
        self.assertTrue(self.sync(dry_run=True)[0].startswith("would move 1 commits to c1"))
        result = self.sync(docs_paths=["*.md", ".coderabbit.yaml"])[0]
        self.assertTrue(result.startswith("moved 3 commits to c3 (docs only on top of c1"), result)

    def test_docs_run_stops_at_a_code_commit(self):
        self.gh.checks = {"c1": [run("build")]}
        self.gh.files = {"c3": ["README.md"]}  # c2 has no checks and changes code
        result = self.sync(docs_paths=["*.md"])[0]
        self.assertTrue(result.startswith("moved 1 commits to c1 ("), result)

    def test_docs_run_must_sit_directly_on_green(self):
        # c3 docs-only, c2 failed, c1 green: c3 is not taken
        self.gh.checks = {"c2": [run("build", "failure")], "c1": [run("build")]}
        self.gh.files = {"c3": ["README.md"]}
        result = self.sync(docs_paths=["*.md"])[0]
        self.assertTrue(result.startswith("moved 1 commits to c1 ("), result)

    def test_too_new_docs_commit_waits(self):
        self.gh.commits[0] = ("c3", ["c2"], NEW)
        self.gh.checks = {"c2": [run("build")]}
        self.gh.files = {"c3": ["README.md"]}
        self.assertTrue(self.sync(docs_paths=["*.md"])[0].startswith("moved 2 commits to c2 ("))

    def test_docs_only_on_top_of_fork_commit_are_taken(self):
        self.gh.fork_sha = "c1"
        self.gh.files = {"c3": [".coderabbit.yaml"], "c2": ["README.md"]}
        self.assertTrue(self.sync()[0].startswith("waiting"))
        result, before, after = self.sync(docs_paths=["*.md", ".coderabbit.yaml"])
        self.assertTrue(result.startswith("moved 2 commits to c3 (docs only on top of c1"), result)
        self.assertEqual(self.gh.patches, [{"sha": "c3", "force": False}])

    def test_code_commit_above_fork_blocks_docs(self):
        self.gh.fork_sha = "c1"
        self.gh.files = {"c3": ["README.md"]}  # c2 changes code, no checks
        self.assertTrue(self.sync(docs_paths=["*.md"])[0].startswith("waiting"))

    def test_docs_only_above_failed_commit_waits(self):
        self.gh.checks = {"c1": [run("build", "failure")]}
        self.gh.files = {"c3": ["README.md"], "c2": ["README.md"]}
        self.assertTrue(self.sync(docs_paths=["*.md"])[0].startswith("waiting"))

    def test_require_checks(self):
        self.gh.checks = {"c3": [run("lint")], "c2": [run("lint"), run("test (ubuntu)")]}
        self.assertTrue(self.sync(require_checks=["test*"])[0].startswith("moved 2 commits to c2"))

    def test_commit_status_must_be_success(self):
        self.gh.checks = {"c3": [run("build")], "c2": [run("build")]}
        self.gh.statuses = {"c3": ("pending", 1), "c2": ("success", 1)}
        self.assertTrue(self.sync()[0].startswith("moved 2 commits to c2"))

    def test_status_only_commit_can_be_green(self):
        self.gh.statuses = {"c3": ("success", 2)}
        self.assertTrue(self.sync()[0].startswith("moved 3 commits to c3"))

    def test_check_runs_are_paginated(self):
        self.gh.checks = {"c3": [run(f"job{i}") for i in range(100)] + [run("job100", "failure")]}
        self.assertTrue(self.sync()[0].startswith("waiting"))

    def test_dry_run_changes_nothing(self):
        self.gh.checks = {"c3": [run("build")]}
        result, _, after = self.sync(dry_run=True)
        self.assertTrue(result.startswith("would move 3 commits to c3"))
        self.assertEqual(after, "c3")
        self.assertEqual(self.gh.patches, [])

    def test_missing_object_falls_back_to_git_push(self):
        self.gh.checks = {"c3": [run("build")]}
        self.gh.patch_error = 'gh: Object does not exist (HTTP 422)'
        with mock.patch.object(sync.Syncer, "push_with_git") as push:
            result = self.sync()[0]
        push.assert_called_once_with("r", "c3")
        self.assertTrue(result.startswith("moved"))

    def test_other_patch_errors_are_raised(self):
        self.gh.checks = {"c3": [run("build")]}
        self.gh.patch_error = "gh: Resource not accessible by personal access token (HTTP 403)"
        with self.assertRaises(RuntimeError):
            self.sync()


class CliTests(unittest.TestCase):
    def test_split_list(self):
        self.assertEqual(sync.split_list("a, b\n# comment\n c  # note\n\n"), ["a", "b", "c"])
        self.assertEqual(sync.split_list(None), [])

    def test_owners_and_repos_are_required(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SYNC_")}
        for argv in (["sync.py", "--fork", "f", "r"], ["sync.py", "--upstream", "u", "--fork", "f"]):
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "argv", argv), \
                    mock.patch("sys.stderr"), self.assertRaises(SystemExit) as e:
                sync.main()
            self.assertEqual(e.exception.code, 2)

    def test_one_failing_repo_does_not_stop_the_others(self):
        calls = []

        def fake_sync(self, repo):
            calls.append(repo)
            if repo == "bad":
                raise RuntimeError("boom")
            return "up to date", "abc", None
        argv = ["sync.py", "--upstream", "u", "--fork", "f", "bad", "good"]
        with mock.patch.object(sync.Syncer, "sync", fake_sync), mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {}, clear=False), mock.patch("builtins.print"), \
                self.assertRaises(SystemExit) as e:
            os.environ.pop("GITHUB_OUTPUT", None)
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            sync.main()
        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(e.exception.code, 1)


class MetadataTests(unittest.TestCase):
    """action.yml, sync.py and the README inputs table must describe the same options."""

    def setUp(self):
        self.action, self.readme, self.script = (read(f) for f in ("action.yml", "README.md", "sync.py"))
        inputs = self.action.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
        self.inputs = re.findall(r"^  ([a-z-]+):$", inputs, re.M)

    def test_every_input_is_documented(self):
        self.assertTrue(self.inputs)
        table = self.readme.split("## Inputs", 1)[1].split("\n## ", 1)[0]
        documented = re.findall(r"^\| `([a-z-]+)` \|", table, re.M)
        self.assertEqual(documented, self.inputs)

    def test_every_env_var_is_read_by_the_script(self):
        for var in set(re.findall(r"\b(SYNC_[A-Z_]+):", self.action)):
            self.assertIn(f'"{var}"', self.script, var)

    def test_every_input_is_passed_to_the_script(self):
        for name in self.inputs:
            if name != "token":
                self.assertIn("${{ inputs.%s }}" % name, self.action, name)


if __name__ == "__main__":
    unittest.main()
