import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nightly_complete.py"
SHA = "0123456789abcdef" * 2 + "01234567"
REPO = "example/agent-usage"
VERSION = "0.1.0.dev42+g0123456789ab"
STAMPED_AT = "2026-11-22T00:05:44Z"
PUBLISHED_AT = "2026-11-22T00:05:45Z"
NOTES = (
    f"Rolling development build {VERSION} from {REPO}@{SHA}. "
    "Verified on the minimum and development Python versions. "
    f"Build: https://github.com/{REPO}/actions/runs/1234"
)


def assets(version):
    return [
        {"name": f"agent_usage-{version}{ext}", "size": 321, "state": "uploaded"}
        for ext in (".tar.gz", "-py3-none-any.whl")
    ]


@pytest.fixture
def release():
    return {
        "tag_name": "nightly",
        "draft": False,
        "prerelease": True,
        "published_at": PUBLISHED_AT,
        "body": NOTES,
        "assets": assets(VERSION),
    }


@pytest.fixture
def tag():
    return {
        "tag": "nightly",
        "message": NOTES + "\n",
        "object": {"type": "commit", "sha": SHA},
        "tagger": {
            "name": "github-actions[bot]",
            "email": "41898282+github-actions[bot]@users.noreply.github.com",
            "date": STAMPED_AT,
        },
    }


@pytest.fixture
def state(release, tag):
    return {"releases": [release], "tag": tag}


def invoke(payload, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


def check_plan(state):
    result = invoke(state, SHA, REPO)
    result.check_returncode()
    return result.stdout.strip()


def check_publish(state, version=VERSION):
    result = invoke(state, SHA, REPO, version)
    result.check_returncode()
    return result.stdout.strip()


def test_complete_release_and_annotated_tag(state):
    assert check_plan(state) == "true"


def test_missing_or_multiple_releases_not_complete(state, release):
    assert check_plan({"releases": [], "tag": state["tag"]}) == "false"
    assert check_plan({"releases": [release, release], "tag": state["tag"]}) == "false"


def test_lightweight_or_missing_tag_not_complete(state):
    state["tag"] = None
    assert check_plan(state) == "false"


@pytest.mark.parametrize(
    ("mutator"),
    [
        lambda t: t.update(tag="stable"),
        lambda t: t["object"].update(type="tag"),
        lambda t: t["object"].update(sha="a" * 40),
        lambda t: t["tagger"].update(date=""),
        lambda t: t["tagger"].update(date="2026-11-22 00:05:10 UTC"),
        lambda t: t["tagger"].update(date="٢٠٢٦-11-22T00:05:10Z"),
        lambda t: t.update(message="Nightly\n"),
        lambda t: t.update(message=t["message"].replace("dev42", "dev43")),
    ],
)
def test_annotated_tag_must_match_published_build(state, mutator):
    mutator(state["tag"])
    assert check_plan(state) == "false"


def test_interrupted_forced_rebuild_same_sha(state):
    state["releases"][0]["assets"] = assets(VERSION.replace("dev42", "dev43"))
    assert check_plan(state) == "false"


@pytest.mark.parametrize(("old", "new"), [(REPO, "another/fork"), (SHA, SHA[:12] + "a" * 28)])
def test_notes_must_identify_exact_source(state, old, new):
    state["releases"][0]["body"] = state["releases"][0]["body"].replace(old, new)
    state["tag"]["message"] = state["tag"]["message"].replace(old, new)
    assert check_plan(state) == "false"


def test_version_must_identify_selected_commit(state):
    other_version = VERSION.replace(SHA[:12], "a" * 12)
    state["releases"][0]["body"] = state["releases"][0]["body"].replace(VERSION, other_version)
    state["releases"][0]["assets"] = assets(other_version)
    state["tag"]["message"] = state["tag"]["message"].replace(VERSION, other_version)
    assert check_plan(state) == "false"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag_name", "stable"),
        ("draft", True),
        ("prerelease", False),
        ("published_at", None),
        ("published_at", ""),
        ("published_at", "2026-11-22T00:05:45+00:00"),
        ("body", None),
        ("body", ""),
        ("assets", assets(VERSION)[:1]),
        ("assets", assets(VERSION) + assets(VERSION)[:1]),
        ("assets", assets(VERSION)[:1] * 2),
        ("assets", [assets(VERSION)[0], assets(VERSION.replace("dev42", "dev43"))[1]]),
    ],
)
def test_only_complete_published_nightly_qualifies(state, field, value):
    state["releases"][0][field] = value
    assert check_plan(state) == "false"


@pytest.mark.parametrize(("field", "value"), [("state", "starter"), ("size", 0)])
def test_assets_must_be_uploaded_and_nonempty(state, field, value):
    state["releases"][0]["assets"][0][field] = value
    assert check_plan(state) == "false"


def test_upload_when_no_nightly_release_exists():
    assert check_publish({"releases": [], "tag": None}) == "upload"


def test_multiple_nightly_releases_fail_closed(release, tag):
    assert invoke({"releases": [release, release], "tag": tag}, SHA, REPO, VERSION).returncode != 0


def test_invalid_publish_payload_fails_closed(release):
    assert invoke([release], SHA, REPO, VERSION).returncode != 0


@pytest.mark.parametrize(("candidate_run", "decision"), [(9, "skip"), (100, "upload")])
def test_compare_advertised_run_numerically(state, candidate_run, decision):
    state["releases"][0]["assets"] = []  # Notes, not asset completeness, establish publish order.
    assert check_publish(state, VERSION.replace("dev42", f"dev{candidate_run}")) == decision


def test_upload_over_other_commit(state):
    old_body = state["releases"][0]["body"].replace(SHA, "a" * 40).replace(SHA[:12], "a" * 12)
    state["releases"][0]["body"] = old_body
    assert check_publish(state, VERSION.replace("dev42", "dev9")) == "upload"


@pytest.mark.parametrize("body", [None, ""])
def test_can_upload_to_empty_draft(state, body):
    state["releases"][0].update(draft=True, published_at=None, body=body)
    assert check_publish(state) == "upload"


@pytest.mark.parametrize("stale", [False, True])
def test_resume_preserves_matching_annotated_tag_across_rerun(state, stale):
    # Re-running all jobs on an already-published forced run produces the same
    # VERSION at a later wall-clock time; resume-clean keeps the original tagger
    # date matching the published release and assets.
    state["tag"]["tagger"]["date"] = "2026-11-22T00:05:10Z"
    if stale:
        state["releases"][0]["assets"] += assets(VERSION.replace("dev42", "dev9"))
        assert check_plan(state) == "false"  # Plan still requires a clean release.
    assert check_publish(state) == "resume-clean"


@pytest.mark.parametrize(
    "tag_mutator",
    [
        lambda s: s.update(tag=None),
        lambda s: s["tag"].update(message=s["tag"]["message"].replace("dev42", "dev41")),
        lambda s: s["tag"]["object"].update(sha="a" * 40),
    ],
)
def test_resume_repairs_missing_or_outdated_annotated_tag(state, tag_mutator):
    tag_mutator(state)
    assert check_plan(state) == "false"
    assert check_publish(state) == "resume-tag"


@pytest.mark.parametrize("issue", ["missing", "empty", "incomplete", "duplicate"])
def test_never_reupload_broken_advertised_version(state, issue):
    entries = state["releases"][0]["assets"]
    if issue == "missing":
        entries.pop()
    elif issue == "empty":
        entries[0]["size"] = 0
    elif issue == "incomplete":
        entries[0]["state"] = "starter"
    else:
        entries.append(entries[0])
    assert invoke(state, SHA, REPO, VERSION).returncode != 0


@pytest.mark.parametrize(
    "issue", ["blank", "duplicate", "fork", "commit", "conflict", "unpublished"]
)
def test_ambiguous_release_fails_closed(state, issue):
    release = state["releases"][0]
    if issue == "unpublished":
        release["published_at"] = None
    else:
        body = release["body"]
        release["body"] = {
            "blank": "",
            "duplicate": body + "\n" + body,
            "fork": body.replace(REPO, "other/fork"),
            "commit": body.replace(SHA, "f" * 40),
            "conflict": body.replace("0.1.0", "0.2.0"),
        }[issue]
    assert invoke(state, SHA, REPO, VERSION).returncode != 0


def test_verify_staged_draft():
    staged = {"draft": True, "prerelease": True, "assets": assets(VERSION)}
    assert invoke(staged, "--verify-staged", VERSION).returncode == 0


@pytest.mark.parametrize(
    ("mutator"),
    [
        lambda d: d.update(draft=False),
        lambda d: d.update(prerelease=False),
        lambda d: d["assets"].pop(),
        lambda d: d["assets"].append(assets(VERSION.replace("dev42", "dev9"))[0]),
        lambda d: d["assets"][0].update(size=0),
        lambda d: d["assets"][0].update(state="starter"),
    ],
)
def test_verify_staged_draft_rejects_invalid_state(mutator):
    staged = {"draft": True, "prerelease": True, "assets": assets(VERSION)}
    mutator(staged)
    assert invoke(staged, "--verify-staged", VERSION).returncode != 0
