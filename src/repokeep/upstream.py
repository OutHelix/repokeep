import base64
import binascii
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import __version__
from .common import CheckError, clean

API = "https://rdb.altlinux.org/api"
OSV_API = "https://api.osv.dev/v1"
PROGRAM = "repokeep"
PROGRAM_VERSION = __version__


def github_repo(value, allow_short=False):
    value = value.strip().strip("\"'")
    value = re.sub(r"^(?:scm:git:|git\+)", "", value)
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:") :]
    if allow_short and re.fullmatch(r"[\w.-]+/[\w.-]+(?:\.git)?", value):
        value = "https://github.com/" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.hostname not in (
        "github.com",
        "www.github.com",
        "codeload.github.com",
        "raw.githubusercontent.com",
    ):
        return None
    pieces = parsed.path.strip("/").split("/")
    if len(pieces) < 2:
        return None
    owner, repo = pieces[:2]
    repo = re.sub(r"\.git$", "", repo.rstrip(".,;)"))
    if not all(
        re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in (".", "..")
        for p in (owner, repo)
    ):
        return None
    if owner.lower() in ("features", "topics", "orgs", "settings", "sponsors"):
        return None
    return owner + "/" + repo


def repos_in_text(text):
    urls = re.findall(
        r"(?:https?://|git://|git@)"
        r"[^\s\"'<>]+",
        text,
    )
    return sorted({repo for url in urls if (repo := github_repo(url))})


def expand_macros(text, macros):
    pattern = r"%\{([A-Za-z_]\w*)\}|%([A-Za-z_]\w*)"
    for _ in range(12):
        new = re.sub(pattern, lambda m: macros.get(m[1] or m[2], m[0]), text)
        if new == text:
            break
        text = new
    return text


def spec_metadata(spec, package):
    macros = {"name": package["name"], "version": package["version"]}
    fields = {"vcs": [], "url": [], "source": []}
    for line in spec.splitlines():
        if re.match(
            r"^\s*%(?:description|package|prep|build|install|check|files|changelog)\b",
            line,
        ):
            break
        definition = re.match(r"\s*%(?:global|define)\s+(\w+)\s+(.+)", line)
        if definition:
            macros[definition[1]] = expand_macros(definition[2].strip(), macros)
        metadata = re.match(r"\s*(VCS|URL|Source0?)\s*:\s*(.+)", line, re.I)
        if metadata:
            key = (
                "source"
                if metadata[1].lower().startswith("source")
                else metadata[1].lower()
            )
            fields[key].append(metadata[2].split(" #", 1)[0].strip())
    fields = {
        key: [expand_macros(value, macros) for value in values]
        for key, values in fields.items()
    }
    return fields, macros


def repo_from_spec(spec, package):
    fields, _ = spec_metadata(spec, package)
    return repo_from_fields(fields)


def repo_from_fields(fields):
    for key in ("vcs", "url", "source"):
        found = set()
        for value in fields[key]:
            found.update(repos_in_text(value))
        if len(found) == 1:
            return found.pop()
        if len(found) > 1:
            raise CheckError(
                "several GitHub repositories in spec " + key + "; use --repo"
            )
        if key == "vcs" and fields[key]:
            return None
    return None


class SourceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.active = [
                attrs.get("href", ""),
                attrs.get("title", "") + " " + attrs.get("aria-label", ""),
            ]

    def handle_data(self, text):
        if self.active:
            self.active[1] += " " + text

    def handle_endtag(self, tag):
        if tag == "a" and self.active:
            self.links.append(self.active)
            self.active = None


class Client:
    def __init__(self, timeout):
        self.timeout = timeout
        self.deadline = None
        self.spec_aliases = []
        self.repo_note = ""
        self.repo_confident = False
        self.source_fields = {}

    def request(self, url, payload=None):
        headers = {
            "User-Agent": PROGRAM + "/" + PROGRAM_VERSION,
            "Accept": "application/json, text/html;q=0.8",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        deadline = time.monotonic() + self.timeout
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CheckError(
                    "HTTP request timed out: " + urllib.parse.urlparse(url).netloc
                )
            try:
                with urllib.request.urlopen(request, timeout=remaining) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise CheckError("HTTP response exceeds 16 MiB")
                return data.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(min(attempt + 1, max(0, deadline - time.monotonic())))
                    continue

                raise CheckError(
                    "HTTP "
                    + str(exc.code)
                    + " from "
                    + urllib.parse.urlparse(url).hostname,
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(min(attempt + 1, max(0, deadline - time.monotonic())))
                    continue
                raise CheckError("network error: " + clean(str(exc))) from None

    def json(self, endpoint, **params):
        url = API + endpoint + "?" + urllib.parse.urlencode(params)
        try:
            value = json.loads(self.request(url))
            if not isinstance(value, dict):
                raise CheckError("invalid response object from ALTRepo API")
            return value
        except ValueError:
            raise CheckError("invalid JSON from ALTRepo API") from None

    def osv(self, endpoint, payload=None):
        try:
            value = json.loads(self.request(OSV_API + endpoint, payload))
        except ValueError:
            raise CheckError("invalid JSON from OSV") from None
        if not isinstance(value, dict):
            raise CheckError("invalid response object from OSV")
        return value

    def tags(self, repo):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false")
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "core.askPass=",
                "-c",
                "http.lowSpeedLimit=1",
                "-c",
                "http.lowSpeedTime=" + str(self.timeout),
                "ls-remote",
                "--tags",
                "--refs",
                "https://github.com/" + repo + ".git",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.kill()

            raise CheckError("GitHub tags request timed out") from None
        finally:
            process.stdout.close()
            process.stderr.close()
            process.wait(timeout=1)
        if process.returncode:
            if re.search(r"could not read Username|Repository not found", stderr, re.I):
                raise CheckError(
                    "GitHub repository unavailable (missing/private or access denied); use --repo"
                )
            raise CheckError("git: " + clean(stderr)[:220])
        tags = sorted(
            {
                line.split("\t", 1)[1][len("refs/tags/") :]
                for line in stdout.splitlines()
                if "\trefs/tags/" in line and not line.endswith("^{}")
            }
        )
        if not tags:
            raise CheckError("GitHub repository has no tags")
        return tags

    def resolve_repo(self, package, rule, branch):
        if rule.get("repo"):
            self.repo_confident = True
            return rule["repo"], "config"
        try:
            response = self.json(
                "/package/specfile_by_name", branch=branch, name=package["name"]
            )
            spec = base64.b64decode(response["specfile_content"], validate=True).decode(
                "utf-8", errors="replace"
            )
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise CheckError("invalid specfile response from ALTRepo API") from None
        except CheckError as exc:
            repo = github_repo(package.get("url") or "")
            if repo:
                self.repo_note = "spec unavailable: " + str(exc)
                return repo, "package URL"
            raise
        fields, macros = spec_metadata(spec, package)
        self.source_fields = fields
        self.spec_aliases = [
            value
            for key, value in macros.items()
            if re.fullmatch(
                r"(?:oname|upname|upstream_name|srcname|project|artifactid)", key, re.I
            )
            and re.fullmatch(r"[A-Za-z0-9_.-]+", value)
        ]
        repo = repo_from_fields(fields)
        if repo:
            self.repo_confident = bool(fields["vcs"]) or any(
                repo in repos_in_text(v) for v in fields["source"]
            )
            return repo, "spec"
        if fields["vcs"]:
            return None, "upstream outside GitHub: " + clean(fields["vcs"][0])
        repo = github_repo(package.get("url") or "")
        if repo:
            return repo, "package URL"
        url = package.get("url") or ""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None, ""
        if parsed.hostname == "pypi.org" and parsed.path.startswith("/project/"):
            project = parsed.path.split("/")[2]
            pypi_url = (
                "https://pypi.org/pypi/"
                + urllib.parse.quote(project, safe="")
                + "/json"
            )
            try:
                info = json.loads(self.request(pypi_url))["info"]
                links = info.get("project_urls") or {}
                for label in (
                    "Source",
                    "Source Code",
                    "Repository",
                    "GitHub",
                    "Homepage",
                ):
                    for key, value in links.items():
                        if key.lower() == label.lower() and github_repo(value):
                            return github_repo(value), "PyPI metadata"
            except (ValueError, KeyError, TypeError):
                raise CheckError("invalid PyPI metadata") from None
            return None, ""
        try:
            page = self.request(url)
        except CheckError as exc:
            self.repo_note = "upstream not resolved; homepage: " + str(exc)
            return None, ""
        parser = SourceLinks()
        parser.feed(page)
        repos = set()
        for href, label in parser.links:
            href = urllib.parse.urljoin(url, href)
            link = urllib.parse.urlparse(href)
            if len(link.path.strip("/").split("/")) > 2:
                continue
            repo = github_repo(href)
            if repo and re.search(r"github|source|repository|code", label, re.I):
                repos.add(repo)
        if len(repos) == 1:
            return repos.pop(), "homepage source link"
        if len(repos) > 1:
            raise CheckError("several GitHub source links on homepage; use --repo")
        return None, ""
