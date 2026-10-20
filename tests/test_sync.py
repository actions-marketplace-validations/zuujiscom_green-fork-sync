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


def run(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None}


class FakeGitHub:
    """Just enough of the REST API for one upstream repo and its fork."""

    def __init__(self, commits, fork_sha):
        # commits: list of (sha, [parent shas], date), newest first, as the commits API lists them
        self.commits = commits
        self.parents = {s: p for s, p, _ in commits}
        self.fork_sha = fork_sha
        self.checks = {}      # sha -> list of check runs
        self.statuses = {}    # sha -> (state, count)
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
        if m := re.fullmatch(r"repos/up/r/commits/(\w+)/check-runs\?per_page=100&page=(\d+)&filter=latest", path):
            runs, page = self.checks.get(m[1], []), int(m[2])
            return reply({"check_runs": runs[(page - 1) * 100:page * 100]})
        if m := re.fullmatch(r"repos/up/r/commits/(\w+)/status", path):
            state, count = self.statuses.get(m[1], ("pending", 0))
            return reply({"state": state, "total_count": count})
        if m := re.fullmatch(r"repos/up/r/compare/(\w+)\.\.\.(\w+)", path):
            base, head = m[1], m[2]
            if base == head:
                return reply({"status": "identical", "ahead_by": 0})
            if base in self.ancestors(head):
                ahead = len(self.ancestors(head) - self.ancestors(base))
                return reply({"status": "ahead", "ahead_by": ahead})
            return reply({"status": "diverged", "ahead_by": 0})
        raise AssertionError(f"unexpected API call: {method} {path}")


def cfg(**kw):
    base = dict(upstream="up", fork="fork", branch="main", fork_branch="main", min_age=10,
                lookback=40, ignore_checks=[], require_checks=[], dry_run=False)
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
