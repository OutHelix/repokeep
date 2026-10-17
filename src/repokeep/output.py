import re
import shutil
import sys
import textwrap

from .common import clean, color_enabled, link, paint
from .models import rpm_label

STATUS_COLORS = {
    "OK": "32",
    "UPD": "1;33",
    "MAJOR": "1;35",
    "VULN": "33",
    "SKIP": "90",
    "WARN": "33",
    "ERR": "1;31",
}


def console_width(width=None):
    if width is not None:
        return max(1, width)
    return max(1, min(88, shutil.get_terminal_size((80, 24)).columns))


def wrapped(text, width=None, indent="", continuation=None, code=None, color=False):
    width = console_width(width)
    continuation = indent if continuation is None else continuation
    indent, continuation = indent[: width - 1], continuation[: width - 1]
    lines = textwrap.wrap(
        clean(text),
        width=width,
        initial_indent=indent,
        subsequent_indent=continuation,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return "\n".join(paint(line, code, color) if code else line for line in lines)


def wrapped_parts(parts, width=None, indent="", continuation=None, color=False):
    width = console_width(width)
    plain = "".join(part[0] for part in parts)
    spans, offset = [], 0
    for part in parts:
        text, code = part[:2]
        url = part[2] if len(part) > 2 else None
        spans.append((offset, offset + len(text), code, url))
        offset += len(text)
    continuation = indent if continuation is None else continuation
    indent, continuation = indent[: width - 1], continuation[: width - 1]
    lines = textwrap.wrap(
        plain,
        width=width,
        initial_indent=indent,
        subsequent_indent=continuation,
        break_on_hyphens=False,
    )
    rendered, offset = [], 0
    for index, line in enumerate(lines):
        padding = indent if index == 0 else continuation
        content = line[len(padding) :]
        start = plain.index(content, offset)
        end = start + len(content)
        pieces = [padding]
        for left, right, code, url in spans:
            left, right = max(start, left), min(end, right)
            if left < right:
                text = link(plain[left:right], url, color)
                pieces.append(paint(text, code, color) if code else text)
        rendered.append("".join(pieces))
        offset = end
    return "\n".join(rendered)


def advisory_ids(vuln):
    ids = {
        clean(identifier)
        for identifier in [vuln["id"]] + vuln.get("aliases", [])
        if identifier.startswith("CVE-")
    }

    return sorted(ids) or [clean(vuln["id"])]


def security_warning(args):
    if not args.vulnerable_only:
        return
    message = (
        "WARNING: OSV checks the upstream version, not the contents of the ALT RPM.\n"
        "ALT may already fix a vulnerability with backported patches without changing Version; false positives are possible.\n"
        "Results may also concern components excluded from the RPM. No OSV match does not prove that a package is safe.\n"
    )
    message = "\n".join(wrapped(line) for line in message.splitlines()) + "\n"
    print(
        paint(message, "1;33", color_enabled(args.color, sys.stderr)),
        file=sys.stderr,
        flush=True,
    )


def render_security(
    result, prefix="", show_repo=False, color=False, width=None, details=False
):
    lines = []

    def add(parts, indent="  ", continuation=None):
        lines.append(wrapped_parts(parts, width, indent, continuation, color))

    add(
        [
            (prefix, "90"),
            ("[" + result.status + "] ", STATUS_COLORS[result.status]),
            (clean(result.name), None),
        ],
        indent="",
        continuation="  ",
    )
    if details:
        add([("ALT RPM: ", "90"), (rpm_label(result.current, result.release), "1")])
        count = len(result.vulnerabilities)
        add(
            [
                (
                    f"Found: {count} upstream "
                    + ("vulnerability" if count == 1 else "vulnerabilities"),
                    "90",
                )
            ]
        )
    else:
        ids = list(
            dict.fromkeys(
                identifier
                for vuln in result.vulnerabilities
                for identifier in advisory_ids(vuln)
            )
        )
        parts = []
        for identifier in ids:
            if parts:
                parts.append((", ", "90"))
            parts.append((identifier, "35"))
        add(parts)
    if result.note:
        add(
            [
                (
                    (
                        "Check incomplete: "
                        if result.status in ("ERR", "WARN")
                        else "Note: "
                    ),
                    "33",
                ),
                (clean(result.note), None),
            ],
            continuation="    ",
        )
    for vuln in result.vulnerabilities if details else []:
        lines.append("")
        severity = clean(vuln.get("severity", "UNKNOWN")).upper()
        severity_text = "not rated" if severity == "UNKNOWN" else severity
        severity_color = {
            "LOW": "36",
            "MODERATE": "33",
            "MEDIUM": "33",
            "HIGH": "1;31",
            "CRITICAL": "1;31",
        }.get(severity, "90")
        add(
            [
                (", ".join(advisory_ids(vuln)), "1;35"),
                (" | Severity: ", "90"),
                (severity_text, severity_color),
            ]
        )
        if vuln.get("summary"):
            add(
                [("Issue: ", "90"), (clean(vuln["summary"]), None)],
                indent="    ",
                continuation="           ",
            )

        fixes = {}
        for fixed in vuln.get("fixed_versions", []):
            match = re.fullmatch(r"(.*?) \[(.*)\]", clean(fixed))
            version, component = match.groups() if match else (clean(fixed), "")
            fixes.setdefault(component, []).append(version)
        if fixes:
            for component, versions in fixes.items():
                add(
                    [("Fixed upstream: ", "90"), (", ".join(versions), "1;32")],
                    indent="    ",
                )
                if component:
                    add([("Component: ", "90"), (component, None)], indent="    ")
        else:
            add(
                [("Fixed upstream: ", "90"), ("not listed in OSV", "33")], indent="    "
            )
            if vuln.get("fixed_commits"):
                add(
                    [("Fix: ", "90"), (", ".join(vuln["fixed_commits"]), "32")],
                    indent="    ",
                )
    if show_repo and result.repo:
        lines.append("")
        add(
            [("Repository: ", "90"), ("https://github.com/" + clean(result.repo), "36")]
        )
    return "\n".join(lines)


def security_summary(count, matched, completed, color=False):
    fields = [
        (f"Checked: {completed} packages", None),
        (f"Packages with OSV matches: {matched}", "33" if matched else "32"),
        (f"No OSV matches: {count['OK']}", "32"),
        (f"Skipped: {count['SKIP']}", "90"),
    ]
    if count["WARN"]:
        fields.append((f"Warnings: {count['WARN']}", "33"))
    if count["ERR"]:
        fields.append((f"Failed or incomplete checks: {count['ERR']}", "1;31"))
    return "Summary:\n" + "\n".join(
        wrapped(text, indent="  ", code=code, color=color) for text, code in fields
    )


def render(
    result,
    show_repo=False,
    name_width=0,
    current_width=0,
    done=None,
    total=None,
    color=False,
    width=None,
    details=False,
):
    prefix = ""
    if done is not None and total is not None:
        digits = len(str(total))
        prefix = f"[{done:>{digits}}/{total}]  "
    label = "[" + result.status + "]"
    if result.security:
        return render_security(result, prefix, show_repo, color, width, details)
    marker = (
        "->"
        if result.status == "UPD"
        else ">"
        if result.note.startswith("RPM version newer")
        else "~"
        if "Git snapshot" in result.note
        else "="
        if result.same_major
        else ""
    )
    upstream = clean(result.same_major) if result.same_major else "-"
    upstream_width = max(18, current_width)

    parts = [
        (prefix, "90"),
        (f"{label:<7}", STATUS_COLORS[result.status]),
        ("  " + f"{clean(result.name):<{name_width}}  ", None),
        (f"{clean(result.current):<{current_width}}", "36"),
        ("  ", None),
        (
            f"{marker:>2}  {upstream:<{upstream_width}}",
            STATUS_COLORS[result.status],
        ),
    ]
    notes = []
    if result.newer_majors:
        notes.append(
            (
                "newer major: "
                + ", ".join(clean(v["version"]) for v in result.newer_majors),
                "35",
            )
        )
    if result.note:
        notes.append((clean(result.note), STATUS_COLORS[result.status]))
    if show_repo and result.repo:
        notes.append(("https://github.com/" + clean(result.repo), "36"))
    width = max(1, width if width is not None else shutil.get_terminal_size().columns)
    if sum(len(text) for text, _ in parts) > width:
        lines = [
            wrapped_parts(
                [
                    (prefix, "90"),
                    (label + " ", STATUS_COLORS[result.status]),
                    (clean(result.name), None),
                ],
                width,
                continuation="  ",
                color=color,
            ),
            wrapped_parts(
                [
                    (clean(result.current), "36"),
                    (" " + marker + " " if marker else "  ", "90"),
                    (upstream, STATUS_COLORS[result.status]),
                ],
                width,
                indent="  ",
                color=color,
            ),
        ]
    else:
        full = list(parts)
        for note in notes:
            full.extend([("  |  ", "90"), note])
        if sum(len(text) for text, _ in full) <= width:
            return wrapped_parts(full, width, color=color)
        lines = [wrapped_parts(parts, width, color=color)]
    lines.extend(
        wrapped_parts([note], width, indent="  ", color=color) for note in notes
    )
    return "\n".join(lines)


def print_heading(packages, args):
    filters = (" / " + ", ".join(clean(g) for g in args.group)) if args.group else ""
    checking = (
        f"Checking {len(packages)} packages: {args.maintainer} / {args.branch}{filters}"
    )
    if args.vulnerable_only:
        print(wrapped(checking) + "\n", file=sys.stderr, flush=True)
        return 0, 0
    name_width = max(len("PACKAGE"), *(len(clean(p["name"])) for p in packages))
    branch_label = clean(args.branch.upper())
    current_width = max(
        len(branch_label), *(len(clean(p["version"])) for p in packages)
    )

    prefix = " " * (
        len(f"[{len(packages)}/{len(packages)}]  ") if not args.no_progress else 0
    )
    heading = (
        prefix
        + f"{'STATUS':<7}  {'PACKAGE':<{name_width}}  "
        + f"{branch_label:<{current_width}}      "
        + "UPSTREAM / NOTES"
    )
    width = max(1, shutil.get_terminal_size().columns)
    print(wrapped(checking, width) + "\n", file=sys.stderr, flush=True)
    if not args.json and len(heading) <= width:
        print(
            paint(heading, "1", color_enabled(args.color, sys.stderr)),
            file=sys.stderr,
            flush=True,
        )
    return name_width, current_width
