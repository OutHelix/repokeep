# repokeep

Upstream releases, security advisories and build tasks for ALT Linux maintainers.
Checks GitHub tags, keeps major versions separate, queries OSV and shows Girar/Gyle
subtasks with architecture results, sources and approvals.

## Install

Python 3.9+; Git for release checks. No third-party Python runtime dependencies.
From the project directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## Usage

```sh
repokeep update -m scala --updates-only
repokeep update -m scala --vulnerable-only
repokeep task -m scala --all
repokeep task -m scala -n 5 -w 3
repokeep task show 435564
```

Replace `scala` with your maintainer login. Updates default to Sisyphus; task lists
cover all branches, exclude DONE and show the latest build activity first.
`--all` shows architectures, sources and approvals; `task show ID` enables these
automatically and includes DONE tasks.
Watch intervals start at 3 seconds; press `q` or Ctrl+C to stop.

Add `--json` for JSON, `--no-color` to disable colors, or `--help` for all options.

OSV checks upstream versions and cannot account for ALT backports: a match does
not prove an RPM vulnerable, and no match does not prove safety.
The task API is not a complete archive.

## Tests

Offline suite and optional live API checks, from the project directory:

```sh
python -m unittest discover -s tests -t .
REPOKEEP_LIVE_TESTS=1 python -m unittest tests.live_checks
```

Fixtures are updated manually after review; see the [fixture guide](tests/fixtures/README.md).

Data: [ALTRepo](https://rdb.altlinux.org/api) ·
[Taskoteka](https://git.altlinux.org/tasks/api/openapi.json) ·
[OSV](https://api.osv.dev/v1) · [MIT License](LICENSE)
