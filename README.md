# repokeep

Command-line tools for ALT Linux package maintainers: check upstream releases
and security advisories, and view Girar/Gyle build tasks.

## Features

- **Updates:** compare source packages with GitHub tags, showing the latest release
  in the current major first and higher majors separately. Filter by package,
  branch, RPM category or ACL mode.
- **Security:** query public OSV data without a token; show advisory IDs, severity,
  descriptions and fixed upstream versions.
- **Tasks:** list a maintainer's tasks with every package and subtask, optional
  architecture results, source links, filters, sorting, limits and automatic refresh.

Both commands support JSON output and optional colors.

## Install

Requires Python 3.9+ and Git for GitHub release checks. No third-party Python
runtime dependencies.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## Usage

```sh
repokeep updates -m scala --sort
repokeep updates -m scala -b p11 -g python --updates-only
repokeep updates -m scala --vulnerable-only --vuln-details
repokeep tasks -m scala --arch
repokeep tasks -m scala --all
repokeep tasks -m scala -b sisyphus,p11 -s FAILED,EPERM --watch
repokeep tasks -m scala --json > tasks.json
```

`-m/--maintainer` is required. For updates, it selects packages; for tasks, it
selects the task owner. Updates default to Sisyphus. Tasks default to all branches,
all states except DONE, newest first.
Updates check one package at a time by default; `-j 2` enables two parallel checks
(the maximum).
Tasks use one layout with aligned subtasks and automatic wrapping in narrow terminals.
`--source` keeps package names and adds source names with Git tags in brackets.
Source names are clickable when terminal hyperlinks and colors are supported;
URLs stay hidden. In JSON, `--source` adds a `source` object (`label`, `url`).
`--all` combines `--arch` and `--source`; task filters stay the same.
Only BUILDING and FAILED headers show extra details: the current stage for
BUILDING, and a failed check from the current Girar event log for FAILED.
Architecture results remain independent. Other states, including TESTED and
EPERM, have no stage or check labels. Stage data may lag behind Taskoteka;
a different attempt or state is ignored.
Stage names match the ALT website; unknown stages keep their original identifiers.
For BUILDING and FAILED tasks, architecture results use matching ALTRepo progress
for the same attempt and iteration; old Taskoteka build directories can contain
stale failures. Packages not reached in that attempt have gray architectures.
JSON includes `stage` and `failed_check` when status data is available.

Use `repokeep updates --help` or `repokeep tasks --help` for all options.
Disable colors with `--no-color` or `NO_COLOR`.

Both commands share `-m/--maintainer`, `-b/--branch`, `-p/--package`,
`--timeout`, `--json`, `--color`, `--no-color` and `--version`.
Package names stay plain; statuses carry colors. Both views wrap in narrow terminals.

| Option | `updates` | `tasks` |
| --- | --- | --- |
| `--branch` | One branch; default `sisyphus` | One or comma-separated branches; default `ALL` |
| `--package` | Case-sensitive name/glob | Case-insensitive substring or `*`/`?` glob; selects the whole task |
| `--sort` | Alphabetical package output | `id`, `package`, `state` or `branch` |
| `--repo` | `PACKAGE=OWNER/REPO` GitHub override | Alias for `--branch` |
| `--all` | — | `--arch --source`; keeps state filters |

JSON goes to stdout, diagnostics to stderr. Updates also sends headings and
summaries to stderr; the tasks text report stays together on stdout.

Optional update overrides belong in `~/.config/repokeep/config.json`
(or `$XDG_CONFIG_HOME/repokeep/config.json`). The CLI also accepts `--repo`,
`--tag-regex`, `--current-version` and `--osv-package` overrides.
An existing script config can be used with `--config PATH`.

OSV checks upstream versions, not ALT backports or installed RPM contents.
A match does not prove the RPM is vulnerable; no match does not prove safety.
The task API is not a complete archive.

## Tests

Run the offline suite with standard-library unittest; Git is replaced by test
stubs, so the suite does not require Git or external services:

```sh
python -m unittest discover -s tests -t . -v
```

External DNS, connections and real Git commands are blocked in tests and Python
child processes. Process integration tests use a loopback HTTP server. Dates and
time zones are fixed; worker completion order is controlled explicitly.

Live checks require Git, network access and explicit opt-in:

```sh
REPOKEEP_LIVE_TESTS=1 python -m unittest tests.live_checks -v
```

They check API contracts for tasks owned by cas/lav/rider and packages maintained
by scala/grenka/cas, plus OSV and GitHub transport. They allow changing versions,
task states and empty selections. Service failures are reported as
`ExternalServiceError` with a nonzero exit status; they are not skipped.
The large viy snapshot is used offline, not queried by the live suite.

To update fixtures, use the URLs and transformations in
[the fixture manifest](tests/fixtures/captures.json). Fetch into a temporary file,
review the response and diff, then update the selected JSON file, SHA-256 hash
and explicit test expectations deliberately. For tags, use
`git ls-remote --tags --refs URL` and preserve all tag names. Ordinary and live
test runs never refresh fixtures or derive replacement expectations automatically.

Data sources: [ALTRepo](https://rdb.altlinux.org/api),
[Taskoteka](https://git.altlinux.org/tasks/api/openapi.json),
[OSV](https://api.osv.dev/v1).

[MIT License](LICENSE) · Evgeniy Serov <scala@altlinux.org>
