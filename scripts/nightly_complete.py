"""Inspect GitHub REST release and tag metadata before building or publishing a nightly."""

import json
import re
import sys

SHA = r"[0-9a-f]{40}"
VERSION = r"[0-9]+(?:\.[0-9]+)*\.dev(?P<run>[0-9]+)\+g(?P<short>[0-9a-f]{12})"
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
HEADER = "Rolling development build "


def valid_timestamp(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(TIMESTAMP, value))


def advertised_text(text: object, repo: str) -> re.Match[str] | None:
    if not isinstance(text, str) or text.count(HEADER) != 1:
        return None
    match = re.match(
        rf"{HEADER}(?P<version>{VERSION}) from {re.escape(repo)}@(?P<sha>{SHA})\.(?=\s|$)",
        text,
    )
    return match if match and match["short"] == match["sha"][:12] else None


def advertised(release: object, repo: str) -> re.Match[str] | None:
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != "nightly"
        or release.get("draft") is not False
        or release.get("prerelease") is not True
        or not valid_timestamp(release.get("published_at"))
    ):
        return None
    return advertised_text(release.get("body"), repo)


def valid_annotated_tag(tag: object, repo: str, version: str, sha: str) -> bool:
    if not isinstance(tag, dict) or tag.get("tag") != "nightly":
        return False
    target = tag.get("object")
    tagger = tag.get("tagger")
    notes = advertised_text(tag.get("message"), repo)
    return bool(
        isinstance(target, dict)
        and target.get("type") == "commit"
        and target.get("sha") == sha
        and isinstance(tagger, dict)
        and valid_timestamp(tagger.get("date"))
        and notes
        and notes["version"] == version
        and notes["sha"] == sha
    )


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


def is_complete(state: object, sha: str, repo: str) -> bool:
    if not isinstance(state, dict):
        return False
    releases = state.get("releases")
    if not isinstance(releases, list) or len(releases) != 1:
        return False
    release = releases[0]
    notes = advertised(release, repo)
    return bool(
        isinstance(release, dict)
        and notes
        and notes["sha"] == sha
        and healthy_assets(release.get("assets"), notes["version"], allow_stale=False)
        and valid_annotated_tag(state.get("tag"), repo, notes["version"], sha)
    )


def publish_decision(state: object, sha: str, repo: str, version: str) -> str:
    candidate = re.fullmatch(VERSION, version)
    if not re.fullmatch(SHA, sha) or not candidate or candidate["short"] != sha[:12]:
        raise ValueError("candidate version must identify the selected commit")
    if not isinstance(state, dict) or not isinstance(releases := state.get("releases"), list):
        raise ValueError("expected nightly release state")
    if not releases:
        return "upload"
    if len(releases) > 1:
        raise ValueError("multiple nightly releases found")
    release = releases[0]
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
        if valid_annotated_tag(state.get("tag"), repo, version, sha):
            return "resume-clean"
        return "resume-tag"
    if notes["sha"] != sha:
        return "upload"
    current, proposed = int(notes["run"]), int(candidate["run"])
    if current == proposed:
        raise ValueError("conflicting versions for the same nightly run")
    return "skip" if current > proposed else "upload"


def verify_staged(release: object, version: str) -> None:
    if not re.fullmatch(VERSION, version):
        raise ValueError("invalid nightly version")
    if (
        not isinstance(release, dict)
        or release.get("draft") is not True
        or release.get("prerelease") is not True
        or not healthy_assets(release.get("assets"), version, allow_stale=False)
    ):
        raise ValueError("staged nightly draft does not have healthy assets")


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        if len(sys.argv) == 3 and sys.argv[1] == "--verify-staged":
            verify_staged(payload, sys.argv[2])
        elif len(sys.argv) == 3:
            print("true" if is_complete(payload, sys.argv[1], sys.argv[2]) else "false")
        elif len(sys.argv) == 4 and sys.argv[1] != "--verify-staged":
            print(publish_decision(payload, sys.argv[1], sys.argv[2], sys.argv[3]))
        else:
            sys.exit("usage: nightly_complete.py SHA REPO [VERSION] | --verify-staged VERSION")
    except (ValueError, json.JSONDecodeError) as exc:
        sys.exit(str(exc))
