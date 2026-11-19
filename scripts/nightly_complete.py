"""Inspect GitHub REST release metadata before building or publishing a nightly."""

import json
import re
import sys

SHA = r"[0-9a-f]{40}"
VERSION = r"[0-9]+(?:\.[0-9]+)*\.dev(?P<run>[0-9]+)\+g(?P<short>[0-9a-f]{12})"
HEADER = "Rolling development build "


def advertised(release: object, repo: str) -> re.Match[str] | None:
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != "nightly"
        or release.get("draft") is not False
        or release.get("prerelease") is not True
    ):
        return None
    body = release.get("body")
    if not isinstance(body, str) or body.count(HEADER) != 1:
        return None
    match = re.match(
        rf"{HEADER}(?P<version>{VERSION}) from {re.escape(repo)}@(?P<sha>{SHA})\.(?=\s|$)",
        body,
    )
    return match if match and match["short"] == match["sha"][:12] else None


def healthy_assets(assets: object, version: str, *, allow_stale: bool) -> bool:
    if not isinstance(assets, list):
        return False
    prefix = f"agent_usage-{version}"
    expected = {prefix + "-py3-none-any.whl", prefix + ".tar.gz"}
    found = set()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(name := asset.get("name"), str):
            return False
        if allow_stale and name not in expected:
            continue
        size = asset.get("size")
        if name in found or asset.get("state") != "uploaded" or type(size) is not int or size <= 0:
            return False
        found.add(name)
    return found == expected


def is_complete(release: object, sha: str, repo: str) -> bool:
    notes = advertised(release, repo)
    return bool(
        isinstance(release, dict)
        and notes
        and notes["sha"] == sha
        and healthy_assets(release.get("assets"), notes["version"], allow_stale=False)
    )


def publish_decision(release: object, sha: str, repo: str, version: str) -> str:
    candidate = re.fullmatch(VERSION, version)
    if not re.fullmatch(SHA, sha) or not candidate or candidate["short"] != sha[:12]:
        raise ValueError("candidate version must identify the selected commit")
    if not isinstance(release, dict):
        raise ValueError("expected a nightly release")
    if (
        release.get("tag_name") == "nightly"
        and release.get("draft") is True
        and release.get("prerelease") is True
        and "body" in release
        and release["body"] in (None, "")
    ):
        return "upload"
    notes = advertised(release, repo)
    if not notes:
        raise ValueError("cannot identify the advertised nightly build")
    if notes["version"] == version:
        if notes["sha"] != sha or not healthy_assets(
            release.get("assets"), version, allow_stale=True
        ):
            raise ValueError("advertised version cannot be safely resumed; dispatch a fresh run")
        return "resume"
    if notes["sha"] != sha:
        return "upload"
    current, proposed = int(notes["run"]), int(candidate["run"])
    if current == proposed:
        raise ValueError("conflicting versions for the same nightly run")
    return "skip" if current > proposed else "upload"


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit("usage: nightly_complete.py SHA REPO [VERSION]")
    sha, repo = sys.argv[1:3]
    try:
        release = json.load(sys.stdin)
        if len(sys.argv) == 4:
            print(publish_decision(release, sha, repo, sys.argv[3]))
        else:
            print("true" if is_complete(release, sha, repo) else "false")
    except ValueError as exc:
        sys.exit(str(exc))
