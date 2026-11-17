# Contributing

## Setup

```sh
uv sync
uv run pre-commit install
```

This project requires uv >= 0.11.24 (see `required-version` in `pyproject.toml`); run
`uv self update` if `just` complains.

## Branches

- `dev` — default branch; push directly here
- `main` — stable releases only; updated via PR from `dev`

## Workflow

```sh
just format      # ruff check --fix + ruff format
just typecheck   # ty check
just unit        # unit tests (no hardware)
just lint        # read-only lint check
just coverage    # unit tests with coverage report
just docs-serve  # browse API docs locally
```

Integration tests require real hardware:

```sh
cp .env.example .env   # fill in PYCOMAP_TEST_HOST
just integration
```

## Releasing

Merging `dev` → `main` is the release. There is no separate tagging workflow: a single workflow,
`.github/workflows/release.yml`, runs on every push to `main` (or manually via *Run workflow*).

### What the workflow does

1. Installs with `uv sync --frozen`, then runs lint (`ruff`), type check (`ty`) and unit tests.
2. Reads `version` from `pyproject.toml` and derives the tag `v<version>`.
3. **If that tag already exists on `origin`, it stops.** Merging to `main` without bumping the
   version is harmless: no tag, no release.
4. Otherwise, in the same run, it creates and pushes the tag, builds with `uv build`, creates a
   GitHub Release (auto-generated notes, built files attached), and publishes to PyPI via trusted
   publishing (no token in the repo).
5. For non-pre-releases it then deploys the docs to GitHub Pages (`deploy-docs.yml`).

Versions with a PEP 440 suffix (`2.4.0rc1`, `a1`, `b1`) are marked as pre-release on GitHub and
PyPI, and their docs are not deployed. The release job runs in the GitHub environment `release`, so
any approval rules configured there apply.

Pull requests are checked by `ci.yml` (lint, type check, unit tests, coverage comment). Integration
tests are not run in CI.

### Choosing the version (semver)

- **Patch** (`x.y.Z`): bug fixes only.
- **Minor** (`x.Y.0`): backward-compatible additions or visible behaviour changes (new API, new
  default timeouts).
- **Major** (`X.0.0`): breaking changes to the public API.

Call out behaviour changes in the PR description: the GitHub Release notes are generated from merged
PRs.

### Steps

Work on `dev`.

1. Make sure everything is green locally: `just format && just typecheck && just unit`.
2. Bump `version` in `pyproject.toml`, then refresh the lockfile so `uv.lock` records it:
   ```sh
   uv lock
   ```
3. If `README.md` or the docs changed, regenerate the AI docs (`llms.txt`, `CLAUDE.md`):
   ```sh
   just ai-docs
   ```
4. Commit, push `dev`, open a PR `dev` → `main`, wait for CI, merge.
5. Watch the **Release** run on `main`. When it finishes, check that the tag, the GitHub Release and
   the new version on [PyPI](https://pypi.org/project/pycomap/) all exist.

### If something goes wrong

- **Pushing a tag by hand does nothing.** The workflow only runs on pushes to `main` and manual
  dispatch.
- **A run fails after the tag was created** (for example the PyPI step): re-running it will *not*
  publish, because the tag now exists and every later step is skipped. Fix the cause, then delete
  the remote tag (and the GitHub Release, if one was created) and re-run the workflow:
  ```sh
  git push origin --delete vX.Y.Z
  ```
- **The run fails before the tag step** (lint, types or tests): nothing was created; fix and merge
  again, or re-run.

### Checklist

- [ ] `just format && just typecheck && just unit` pass
- [ ] `version` bumped in `pyproject.toml`, `uv lock` run
- [ ] PR `dev` → `main` merged, **Release** workflow green
- [ ] Tag, GitHub Release and PyPI version all exist
