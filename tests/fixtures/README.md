# Fixture provenance

Captured from public APIs on 2026-10-17, using the original scripts' requests:

- `altrepo-packages.json`: GET https://rdb.altlinux.org/api/site/maintainer_packages?branch=sisyphus&maintainer_nickname=scala&by_acl=none
- `altrepo-spec.json`: GET https://rdb.altlinux.org/api/package/specfile_by_name?branch=sisyphus&name=flexmark-java
- `taskoteka-list.json`: GET https://git.altlinux.org/tasks/api/tasks?user=scala&repo=ALL&state=ALL
- `taskoteka-detail.json`: GET https://git.altlinux.org/tasks/api/tasks/435564
- `taskoteka-schema.json`: the components/schemas portion of https://git.altlinux.org/tasks/api/openapi.json
- `taskoteka-source-repo.json`: GET https://git.altlinux.org/tasks/api/tasks/437508
- `taskoteka-source-git.json`: GET https://git.altlinux.org/tasks/437508/gears/100/git
- `taskoteka-source-srpm.json`: GET https://git.altlinux.org/tasks/api/tasks/437328
- `taskoteka-source-delete.json`: GET https://git.altlinux.org/tasks/api/tasks/437479
- `osv-query.json`: POST https://api.osv.dev/v1/query with
  `{"package":{"ecosystem":"PyPI","name":"Jinja2"},"version":"3.1.0"}`.

All are unmodified response bodies except the documented schema extraction.
Source link paths were also checked against the HTML pages for these tasks.
Tests supplement these with clearly constructed records for rare boundary cases;
synthetic task IDs and advisory identities in tests are not claims about live data.
The default test suite never queries these remote URLs.

`captures.json` records URLs, capture dates, transformations and SHA-256 hashes
for every JSON fixture. Additional snapshots cover Taskoteka owners cas/lav/rider
(including a large detail response for each), ALTRepo maintainers cas/grenka/viy,
the Development/Java category, and GitHub refs from vsch/flexmark-java,
google/guava and apache/maven. GitHub fixtures contain sorted tag names extracted
from `git ls-remote --tags --refs`, without changing names or selecting releases.

`version-scenarios.json`, `github-parallel.json` and `osv-components.json` are
constructed boundary cases, identified as such in the manifest. They cover
isolated tag families, qualifiers, snapshots, deterministic process coordination,
and multi-component advisory aliases. Their advisory IDs are illustrative.

Tests only read these files. Refresh into a temporary file first, validate and
review it, then replace the intended fixture and update its manifest hash and
explicit assertions together. Never regenerate expected results during a test run.

`altrepo-progress-437142.json` and `altrepo-progress-437493.json` are complete
responses from `/task/progress/task_info/{id}`. `girar-check-logs.json` preserves
complete public Girar event logs with their URLs, including failed noarch and
installation checks and a test-only run with an ignored ACL check. ELF failure
and service-race cases are explicitly constructed in the test code.

`task-stage-labels.json` records the public stage identifiers and English UI
captions used by ALTRepo Frontend. The manifest pins the source revision.
Untranslated stages retain their identifiers, as on the website. No frontend
implementation code is included.

The Taskoteka/ALTRepo pairs for tasks 363755 and 363764 capture stale build
directory results versus current progress. Task 363755 failed on subtask #4;
the event log confirms the remaining packages were not built in attempt 6.1.
Task 363764 also covers a successful subtask before a failure and untouched
subtasks. These snapshots are regression data, independent of live task changes.

`taskoteka-excluded.json` is an unmodified response from
`/tasks/api/tasks/437537`, captured on 2026-10-19. The llama.cpp subtask explicitly
excludes i586 while x86_64 and aarch64 are built; it also has a package approval.
