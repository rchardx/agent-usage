import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nightly_complete.py"
SHA = "0123456789abcdef" * 2 + "01234567"
REPO = "example/agent-usage"
VERSION = "0.1.0.dev42+g0123456789ab"


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
        "body": (
            f"Rolling development build {VERSION} from {REPO}@{SHA}. "
            "Verified on the minimum and development Python versions. "
            f"Build: https://github.com/{REPO}/actions/runs/1234"
        ),
        "assets": assets(VERSION),
    }


def invoke(release, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, SHA, REPO, *args],
        input=json.dumps(release),
        text=True,
        capture_output=True,
    )


def check(release, *args):
    result = invoke(release, *args)
    result.check_returncode()
    return result.stdout.strip()


def test_complete_release(release):
    assert check(release) == "true"


def test_interrupted_forced_rebuild_same_sha(release):
    # Both new assets have been uploaded and old assets deleted, but gh release
    # edit hasn't run: checking only the tag and the filenames would skip repair.
    release["assets"] = assets(VERSION.replace("dev42", "dev43"))
    assert check(release) == "false"


@pytest.mark.parametrize(("old", "new"), [(REPO, "another/fork"), (SHA, SHA[:12] + "a" * 28)])
def test_notes_must_identify_exact_source(release, old, new):
    release["body"] = release["body"].replace(old, new)
    assert check(release) == "false"


def test_version_must_identify_selected_commit(release):
    other_version = VERSION.replace(SHA[:12], "a" * 12)
    release["body"] = release["body"].replace(VERSION, other_version)
    release["assets"] = assets(other_version)
    assert check(release) == "false"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tag_name", "stable"),
        ("draft", True),
        ("prerelease", False),
        ("body", None),
        ("body", ""),
        ("assets", assets(VERSION)[:1]),
        ("assets", assets(VERSION) + assets(VERSION)[:1]),
        ("assets", assets(VERSION)[:1] * 2),
        ("assets", [assets(VERSION)[0], assets(VERSION.replace("dev42", "dev43"))[1]]),
    ],
)
def test_only_complete_published_nightly_qualifies(release, field, value):
    release[field] = value
    assert check(release) == "false"


@pytest.mark.parametrize(("field", "value"), [("state", "starter"), ("size", 0)])
def test_assets_must_be_uploaded_and_nonempty(release, field, value):
    release["assets"][0][field] = value
    assert check(release) == "false"


@pytest.mark.parametrize(("candidate_run", "decision"), [(9, "skip"), (100, "upload")])
def test_compare_advertised_run_numerically(release, candidate_run, decision):
    release["assets"] = []  # Notes, not asset completeness, establish publish order.
    assert check(release, VERSION.replace("dev42", f"dev{candidate_run}")) == decision


def test_upload_over_other_commit(release):
    release["body"] = release["body"].replace(SHA, "a" * 40).replace(SHA[:12], "a" * 12)
    assert check(release, VERSION.replace("dev42", "dev9")) == "upload"


@pytest.mark.parametrize("body", [None, ""])
def test_can_upload_to_empty_draft(release, body):
    release.update(draft=True, body=body)
    assert check(release, VERSION) == "upload"


@pytest.mark.parametrize("stale", [False, True])
def test_resume_healthy_advertised_version(release, stale):
    if stale:
        release["assets"] += assets(VERSION.replace("dev42", "dev9"))
        assert check(release) == "false"  # Plan still requires a clean release.
    assert check(release, VERSION) == "resume"


@pytest.mark.parametrize("issue", ["missing", "empty", "incomplete", "duplicate"])
def test_never_reupload_broken_advertised_version(release, issue):
    entries = release["assets"]
    if issue == "missing":
        entries.pop()
    elif issue == "empty":
        entries[0]["size"] = 0
    elif issue == "incomplete":
        entries[0]["state"] = "starter"
    else:
        entries.append(entries[0])
    assert invoke(release, VERSION).returncode != 0


@pytest.mark.parametrize("issue", ["blank", "duplicate", "fork", "commit", "conflict"])
def test_ambiguous_release_fails_closed(release, issue):
    body = release["body"]
    release["body"] = {
        "blank": "",
        "duplicate": body + "\n" + body,
        "fork": body.replace(REPO, "other/fork"),
        "commit": body.replace(SHA, "f" * 40),
        "conflict": body.replace("0.1.0", "0.2.0"),
    }[issue]
    assert invoke(release, VERSION).returncode != 0
