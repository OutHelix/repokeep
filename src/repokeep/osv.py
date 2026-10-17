import re
import time
import urllib.parse

from .common import CheckError, clean
from .models import vulnerability_result
from .versions import parse_version

SEVERITY_RANK = {
    "UNKNOWN": 0,
    "LOW": 1,
    "MODERATE": 2,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}


def repository_url(value):
    value = str(value).strip().removeprefix("git+").rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    path = parsed.path.removesuffix(".git").rstrip("/")
    if not path or parsed.query or parsed.fragment:
        return None
    if parsed.hostname.lower() == "github.com":
        path = path.lower()
    return "https://" + parsed.netloc.lower() + path


def osv_package(value):
    ecosystem, sep, name = str(value).partition(":")
    if not sep or not ecosystem.strip() or not name.strip():
        raise ValueError("expected ECOSYSTEM:NAME")
    ecosystem, name = ecosystem.strip(), name.strip()
    if re.search(r"[\x00-\x1f\x7f]", ecosystem + name):
        raise ValueError("invalid control character in OSV package")
    if ecosystem == "GIT":
        name = repository_url(name)
        if not name:
            raise ValueError("GIT requires a public HTTP(S) repository URL")
    elif ecosystem == "Maven" and not re.fullmatch(r"[^:\s]+:[^:\s]+", name):
        raise ValueError("Maven requires groupId:artifactId")
    return {"ecosystem": ecosystem, "name": name}


def package_from_url(value):
    parsed = urllib.parse.urlparse(str(value))
    parts = urllib.parse.unquote(parsed.path).strip("/").split("/")
    host = (parsed.hostname or "").lower()
    if (
        host in ("pypi.org", "pypi.python.org")
        and len(parts) >= 2
        and parts[0] == "project"
    ):
        return {"ecosystem": "PyPI", "name": parts[1]}
    if host == "crates.io" and len(parts) >= 2 and parts[0] == "crates":
        return {"ecosystem": "crates.io", "name": parts[1]}
    if (
        host in ("npmjs.com", "www.npmjs.com")
        and len(parts) >= 2
        and parts[0] == "package"
    ):
        return {
            "ecosystem": "npm",
            "name": "/".join(parts[1:3]) if parts[1].startswith("@") else parts[1],
        }
    if (
        host in ("central.sonatype.com", "mvnrepository.com")
        and len(parts) >= 3
        and parts[0] == "artifact"
    ):
        return {"ecosystem": "Maven", "name": parts[1] + ":" + parts[2]}
    return None


def osv_targets(package, rule, client, args, result):
    if rule.get("osv_packages"):
        return rule["osv_packages"]
    native = package_from_url(package.get("url", "")) if not rule.get("repo") else None
    if native:
        return [native]
    result.repo, result.repo_source = client.resolve_repo(package, rule, args.branch)
    if result.repo:
        return [{"ecosystem": "GIT", "name": "https://github.com/" + result.repo}]
    fields = client.source_fields
    for value in fields.get("url", []) + fields.get("source", []):
        native = package_from_url(value)
        if native:
            return [native]
    repositories = {
        url
        for value in fields.get("vcs", [])
        if value
        for url in [repository_url(value.split()[0])]
        if url
    }
    if len(repositories) == 1:
        return [{"ecosystem": "GIT", "name": repositories.pop()}]
    return []


def affected_entries(record, target, related_repo=False):
    affected = record.get("affected", [])
    if not isinstance(affected, list) or any(not isinstance(a, dict) for a in affected):
        raise CheckError("invalid affected packages from OSV")
    for item in affected:
        if item.get("package") is not None and not isinstance(item["package"], dict):
            raise CheckError("invalid affected package identity from OSV")
        if not isinstance(item.get("ranges", []), list) or any(
            not isinstance(span, dict) for span in item.get("ranges", [])
        ):
            raise CheckError("invalid vulnerability ranges from OSV")
    if target["ecosystem"] != "GIT":
        entries = []
        for item in affected:
            identity = item.get("package") or {}
            name, expected = identity.get("name"), target["name"]
            if target["ecosystem"] == "PyPI" and isinstance(name, str):
                name, expected = (
                    re.sub(r"[-_.]+", "-", s).lower() for s in (name, expected)
                )
            if identity.get("ecosystem") == target["ecosystem"] and name == expected:
                entries.append(item)
        return entries
    repo = repository_url(target["name"])
    entries = [
        item
        for item in affected
        if any(
            r.get("type") == "GIT" and repository_url(r.get("repo", "")) == repo
            for r in item.get("ranges", [])
            if isinstance(r, dict)
        )
    ]
    if entries or not related_repo:
        return entries

    references = record.get("references", [])
    if any(
        isinstance(ref, dict)
        and ref.get("type") in ("PACKAGE", "SOURCE", "WEB")
        and repository_url(ref.get("url", "")) == repo
        for ref in references
    ):
        identities = {
            (
                item.get("package", {}).get("ecosystem"),
                item.get("package", {}).get("name"),
            )
            for item in affected
            if isinstance(item.get("package"), dict)
        }
        if len(identities) == 1:
            return affected
    return []


def fixed_releases(events, version):
    current = parse_version(version)
    start = None
    start_known = True
    fixed = []
    for event in events:
        if not isinstance(event, dict):
            raise CheckError("invalid vulnerability range event")
        if "introduced" in event:
            raw = event["introduced"]
            start = None if raw == "0" else parse_version(str(raw))
            start_known = raw == "0" or start is not None
        if "fixed" in event:
            raw = str(event["fixed"])
            end = parse_version(raw)
            if (
                current is None
                or end is None
                or not start_known
                or (
                    (start is None or start.key <= current.key)
                    and current.key < end.key
                )
            ):
                fixed.append(raw)
            start, start_known = None, True
        elif "last_affected" in event or "limit" in event:
            start, start_known = None, True
    return fixed


def severity_label(record, entries):
    labels = []
    for obj in [record] + entries:
        for key in ("database_specific", "ecosystem_specific"):
            data = obj.get(key) or {}
            if isinstance(data, dict) and isinstance(data.get("severity"), str):
                labels.append(data["severity"].upper())
    return max(
        (label for label in labels if label in SEVERITY_RANK),
        key=SEVERITY_RANK.get,
        default="UNKNOWN",
    )


def add_osv_record(result, record, target, version, related_repo=False):
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("id"), str)
        or not record["id"]
    ):
        raise CheckError("invalid vulnerability identifier from OSV")
    if record.get("withdrawn"):
        return
    entries = affected_entries(record, target, related_repo)
    if not entries:
        return
    aliases = record.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
        raise CheckError("invalid vulnerability aliases from OSV")
    ids = set(aliases) | {record["id"]}
    versions, commits = set(), set()
    for entry in entries:
        identity = entry.get("package") or {}
        component = (
            str(identity.get("ecosystem", "")) + ":" + str(identity.get("name", ""))
        )
        for span in entry.get("ranges", []):
            if not isinstance(span, dict) or not isinstance(
                span.get("events", []), list
            ):
                raise CheckError("invalid vulnerability range from OSV")
            events = span.get("events", [])
            if span.get("type") in ("SEMVER", "ECOSYSTEM"):
                for fixed in fixed_releases(events, version):
                    versions.add(
                        clean(fixed)
                        + (" [" + clean(component) + "]" if component != ":" else "")
                    )
            elif span.get("type") == "GIT" and repository_url(
                span.get("repo", "")
            ) == repository_url(target["name"]):
                for event in events:
                    fixed = event.get("fixed") if isinstance(event, dict) else None
                    if isinstance(fixed, str) and re.fullmatch(
                        r"[0-9a-fA-F]{40,64}", fixed
                    ):
                        commits.add("commit " + fixed[:12])
    matches = [v for v in result.vulnerabilities if set(v["aliases"]) & ids]
    severity = max(
        [v["severity"] for v in matches] + [severity_label(record, entries)],
        key=SEVERITY_RANK.get,
    )
    if matches:
        vuln = matches[0]
        for duplicate in matches[1:]:
            for field in ("aliases", "fixed_versions", "fixed_commits"):
                vuln[field].extend(duplicate[field])
            if not vuln.get("summary"):
                vuln["summary"] = duplicate.get("summary", "")
            result.vulnerabilities.remove(duplicate)
    else:
        vuln = {
            "id": record["id"],
            "aliases": [],
            "severity": "UNKNOWN",
            "summary": "",
            "fixed_versions": [],
            "fixed_commits": [],
            "fixed": [],
        }
        result.vulnerabilities.append(vuln)
    description = record.get("summary") or record.get("details")
    if not vuln.get("summary") and isinstance(description, str):
        vuln["summary"] = clean(description)
    vuln["aliases"] = sorted(set(vuln["aliases"]) | ids)
    vuln["id"] = min(
        vuln["aliases"],
        key=lambda s: (
            0 if s.startswith("CVE-") else 1 if s.startswith("GHSA-") else 2,
            s,
        ),
    )
    vuln["severity"] = severity
    vuln["fixed_versions"] = sorted(set(vuln["fixed_versions"]) | versions)
    vuln["fixed_commits"] = sorted(set(vuln["fixed_commits"]) | commits)
    vuln["fixed"] = vuln["fixed_versions"] or vuln["fixed_commits"]
    result.status = "VULN"


def check_vulnerable_package(package, rule, client, args, progress=None):
    result = vulnerability_result(package)
    client.deadline = time.monotonic() + max(0.1, args.package_timeout - 0.5)
    looked_up = set()
    try:
        version = str(rule.get("current_version", result.current)).strip()
        if "current_version" not in rule and re.search(
            r"[._-]git[0-9a-f]{7,40}$", version, re.I
        ):
            result.status, result.note = (
                "SKIP",
                "Git snapshot: set current_version and --osv-package for an upstream release",
            )
            return result
        targets = osv_targets(package, rule, client, args, result)
        if not targets:
            result.status, result.note = (
                "SKIP",
                "upstream identity unavailable; use --osv-package PACKAGE=ECOSYSTEM:NAME",
            )
            return result
        if version != result.current:
            result.note = "checked upstream version " + clean(version)
        for target in targets:
            token, visited = None, set()
            while True:
                payload = {"package": target, "version": version}
                if token:
                    payload["page_token"] = token
                response = client.osv("/query", payload)
                records = response.get("vulns", [])
                if not isinstance(records, list):
                    raise CheckError("invalid vulnerability list from OSV")
                for record in records:
                    if (
                        isinstance(record, dict)
                        and not record.get("withdrawn")
                        and not affected_entries(record, target)
                    ):
                        raise CheckError(
                            "OSV returned a vulnerability for a different upstream"
                        )
                    add_osv_record(result, record, target, version)
                if progress:
                    progress(result)
                for record in records:
                    if not isinstance(record, dict) or record.get("withdrawn"):
                        continue
                    for alias in record.get("aliases", []):
                        if (
                            target["ecosystem"] != "GIT"
                            or not alias.startswith("GHSA-")
                            or alias in looked_up
                        ):
                            continue
                        looked_up.add(alias)
                        detail = client.osv(
                            "/vulns/" + urllib.parse.quote(alias, safe="")
                        )
                        add_osv_record(
                            result, detail, target, version, related_repo=True
                        )
                        if progress:
                            progress(result)
                token = response.get("next_page_token")
                if not token:
                    break
                if not isinstance(token, str) or token in visited:
                    raise CheckError("invalid or repeated OSV pagination token")
                visited.add(token)
    except CheckError as exc:
        result.status, result.note = (
            "ERR",
            "; ".join(filter(None, (result.note, str(exc)))),
        )
    return result
