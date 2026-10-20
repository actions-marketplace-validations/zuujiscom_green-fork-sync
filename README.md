# Green fork sync

A GitHub Action that keeps your forks up to date with their upstream repositories, but only
moves a fork to an upstream commit **after every CI check on that commit has passed**.

Other fork-sync tools copy whatever is on upstream `main` right now, even if its CI is red
or still running. This one waits for green, so your fork's `main` is always a commit that
upstream's own CI approved.

- Syncs any number of forks from one workflow.
- Fast-forward only. A fork with commits of its own is reported and never overwritten.
- Works for linked forks and for plain copies of a repo that GitHub doesn't show as forks.
- Python standard library plus the `gh` CLI, both already on GitHub-hosted runners.

## How it decides a commit is green

For each repo it walks the upstream branch's recent history, newest first, and picks the
first commit that:

1. was itself the tip of the upstream branch (first-parent history, so commits from merged
   PR branches don't count),
2. is at least `min-age-minutes` old (default 10), so every workflow has had time to start,
3. has at least one check run or commit status,
4. has every check run finished as **success**, **skipped** or **neutral**, with nothing
   failed, cancelled, timed out or still running (names in `ignore-checks` are left out),
5. has a combined commit status of success, if it has any statuses.

The fork's branch is then fast-forwarded to that commit. If no newer commit qualifies yet,
the fork stays where it is and the next run tries again.

Expect some waiting: busy projects often cancel CI on a commit when a newer push arrives,
and commits that only touch docs are often skipped by `paths:` filters, so they have no
checks. Those commits are passed over, and the fork moves on the next fully green one.

## Setup

### 1. Create a token

The default `GITHUB_TOKEN` can't write to other repositories, so the action needs a
fine-grained personal access token:

1. Go to **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token** (<https://github.com/settings/personal-access-tokens/new>).
2. **Resource owner:** the account or organization that owns your forks.
3. **Repository access:** *Only select repositories*, and pick every fork you want synced.
4. **Repository permissions:**
   - **Contents: Read and write** (to move the branch),
   - **Workflows: Read and write** (GitHub requires this whenever an upstream commit
     changes files in `.github/workflows`),
   - **Metadata: Read-only** (required, selected automatically).
5. Generate it and copy the value.

### 2. Store it as a secret

Create a repository to run the sync from (a small repo of its own works well), then add
the token as a secret there, for example named `SYNC_TOKEN`:

```bash
gh secret set SYNC_TOKEN -R <you>/<sync-repo>
```

It prompts for the value, so the token doesn't end up in your shell history.

### 3. Add a workflow

Create `.github/workflows/fork-sync.yml` in that repository:

```yaml
name: Fork sync

on:
  schedule:
    - cron: "*/15 * * * *"   # every 15 minutes
  workflow_dispatch:

jobs:
  sync:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: zuujiscom/green-fork-sync@v1
        with:
          token: ${{ secrets.SYNC_TOKEN }}
          upstream-owner: some-org        # owner of the original repos
          repos: |
            first-repo
            second-repo
```

Each name must be the same upstream and in your fork (`some-org/first-repo` →
`<fork-owner>/first-repo`). `fork-owner` defaults to the owner of the repository the
workflow runs in.

### 4. Try it

To see what it would do before it changes anything, add `dry-run: "true"` under `with:`,
then start the workflow by hand:

```bash
gh workflow run fork-sync.yml -R <you>/<sync-repo>
```

The run's summary page shows a table with each repo and its result. When it looks right,
remove the `dry-run` line; the schedule takes over from there.

## Inputs

| input | default | description |
|---|---|---|
| `token` | *(required)* | Token that can write to the forks (see Setup). |
| `upstream-owner` | *(required)* | User or organization that owns the original repositories. |
| `fork-owner` | owner of the workflow's repo | User or organization that owns the forks. |
| `repos` | | Repository names, one per line or comma-separated. |
| `repos-file` | | Path to a file with repository names, one per line (`#` comments allowed). Needs `actions/checkout` first. |
| `branch` | `main` | Upstream branch to follow. |
| `fork-branch` | same as `branch` | Branch in the fork to move. |
| `min-age-minutes` | `10` | Skip upstream commits newer than this. |
| `lookback` | `40` | How many recent upstream commits to consider (max 100). |
| `ignore-checks` | | Check-run names to leave out of the decision, `*` wildcards allowed (for example a flaky job, or `Dependabot`). |
| `require-checks` | | Check-run names that must be present and green, same format. |
| `dry-run` | `false` | Only report what would move. |
| `status-file` | | Also write the result table to this Markdown file (commit it yourself if you want it in the repo). |

## Outputs

| output | description |
|---|---|
| `moved` | Number of forks that were moved. |
| `failed` | Number of repos that hit an error. The step also fails when this is above 0. |

## Good to know

- **Cost:** GitHub-hosted runners are free for public repositories. In a private repo each
  run uses about 1–2 billed minutes, so run it less often (every 2 hours is about 750
  minutes a month).
- **Scheduled workflows in public repos are disabled after 60 days without repository
  activity.** If the sync repo itself never changes, re-enable it now and then, or commit
  the `status-file` so each sync counts as activity.
- **Your forks' own CI** will run when the action moves their branch, because the push is
  made with your token. GitHub keeps workflows switched off on new forks until you accept
  them on each fork's **Actions** tab.
- **Diverged forks** (with commits upstream doesn't have) are skipped with a message; sync
  those yourself.
- **Copies that aren't linked forks** are supported: the commit is fetched from upstream
  and pushed without force.

## Run it locally

With the [GitHub CLI](https://cli.github.com/) signed in, a dry run reads the APIs and
changes nothing:

```bash
python3 sync.py --upstream some-org --fork you --dry-run first-repo second-repo
```

`python3 sync.py --help` lists every option; they match the action inputs.

## License

[MIT](LICENSE)
