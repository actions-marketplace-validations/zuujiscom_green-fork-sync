# CLAUDE.md

Notes for Claude (or any coding agent) working in this repository.

## What this is

A composite GitHub Action that fast-forwards forks to the newest upstream commit whose CI
checks all passed. Users call it from their own workflow with `uses: zuujiscom/green-fork-sync@v1`.

- `action.yml`: inputs and outputs; passes every input to the script as a `SYNC_*`
  environment variable and runs `python3 "$GITHUB_ACTION_PATH/sync.py"`.
- `sync.py`: all the logic. Every option works as a CLI flag and as a `SYNC_*` variable.
- `README.md`: the user-facing setup guide and inputs table.

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

There are no unit tests yet. Test against real public repos with a dry run, which only
reads the API (needs `gh auth login`):

```bash
python3 sync.py --upstream <owner> --fork <your-account> --dry-run <repo> [<repo>...]
python3 sync.py --help
```

Pick an upstream with active CI, so you see moved, waiting and up-to-date cases. Before
any run without `--dry-run`, make sure the forks are ones you're allowed to change.
Check that the argument errors still work (no `--upstream`, no repos).

## Releasing

Tags follow semver. After changes are on `main` and tested:

```bash
git tag -a v1.2.3 -m "v1.2.3" && git push origin v1.2.3
git tag -f -a v1 -m "v1" && git push -f origin v1   # move the major tag users pin to
```

Breaking input changes need a new major tag (`v2`), never a moved `v1`.
