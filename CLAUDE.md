# CLAUDE.md

Notes for Claude (or any coding agent) working in this repository.

## What this is

A composite GitHub Action that fast-forwards forks to the newest upstream commit whose CI
checks all passed. Users call it from their own workflow with `uses: zuujiscom/green-fork-sync@v1`.

- `action.yml`: inputs and outputs; passes every input to the script as a `SYNC_*`
  environment variable and runs `python3 "$GITHUB_ACTION_PATH/sync.py"`.
- `sync.py`: all the logic. Every option works as a CLI flag and as a `SYNC_*` variable.
- `README.md`: the user-facing setup guide and inputs table.
- `tests/test_sync.py`: offline tests; `.github/workflows/ci.yml`: CI.

## Rules

- **Never force-push or rewrite a user's branch.** Moves are fast-forward only
  (`PATCH .../git/refs/heads/<branch>` with `force: false`, or a plain `git push` in the
  fallback). A fork with its own commits must be reported and left alone.
- **Never print, log or write the token.** It reaches git only through an
  `http.extraheader` on the one push command.
- **Standard library only.** No `pip install`; the action must run on a stock
  GitHub-hosted runner with nothing but `python3` (3.9+) and `gh`.
- **Keep the three places in sync** when adding or renaming an option: the input in
  `action.yml`, its `SYNC_*` variable and argparse flag in `sync.py`, and the inputs
  table in `README.md`.
- A commit is green only if it has at least one check and nothing is failed, cancelled or
  still running. Don't loosen this by default; add an opt-in input instead.
- One repo failing must not stop the others: errors are caught per repo, counted in the
  `failed` output, and make the step fail at the end.

## Testing

Offline unit tests (no network, no token) replace the GitHub API with an in-memory fake:

```bash
python3 -m unittest discover -s tests -v
```

Add a test for every behaviour change: the fake in `tests/test_sync.py` (`FakeGitHub`)
models commits with parents, check runs, commit statuses, compare and the ref update.
`MetadataTests` fail if `action.yml`, the `SYNC_*` variables in `sync.py` and the README
inputs table drift apart, so update all three together.

`.github/workflows/ci.yml` runs the tests on Python 3.9, 3.12 and the latest 3.x for every
push and PR (and weekly), then runs the action itself as a read-only dry run against real
public repos. To try the real API by hand (needs `gh auth login`; reads only):

```bash
python3 sync.py --upstream <owner> --fork <your-account> --dry-run <repo> [<repo>...]
```

Before any run without `--dry-run`, make sure the forks are ones you're allowed to change.

## Releasing

Tags follow semver. After changes are on `main` and CI is green:

```bash
git tag -a v1.2.3 -m "v1.2.3" && git push origin v1.2.3
git tag -f -a v1 -m "v1" && git push -f origin v1   # move the major tag users pin to
```

Then create a GitHub release from the new tag (keep "Publish this Action to the GitHub
Marketplace" ticked) so the Marketplace listing shows it. Breaking input changes need a new
major tag (`v2`), never a moved `v1`.
