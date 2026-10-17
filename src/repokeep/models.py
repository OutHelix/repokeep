from dataclasses import dataclass, field
from typing import Optional

from .common import clean


@dataclass
class Result:
    name: str
    current: str
    release: str
    status: str = "WARN"
    repo: Optional[str] = None
    repo_source: str = ""
    same_major: Optional[str] = None
    same_major_tag: Optional[str] = None
    newer_majors: list = field(default_factory=list)
    note: str = ""
    security: bool = False
    vulnerabilities: list = field(default_factory=list)


def rpm_label(version, release):
    return clean(version) + ("-" + clean(release) if release else "")


def vulnerability_result(package):
    return Result(
        package["name"],
        package["version"],
        package.get("release", ""),
        status="OK",
        security=True,
    )
