from __future__ import annotations

from fakes import finding

from advpipe.models import AuthorResponse, Finding, Verdict


def test_pass_with_blocking_counts_as_fail() -> None:
    v = Verdict.model_validate({"verdict": "PASS", "findings": [finding()]})
    assert v.verdict == "FAIL"
    assert len(v.blocking) == 1


def test_pass_with_minor_stays_pass() -> None:
    v = Verdict.model_validate({"verdict": "PASS", "findings": [finding(severity="minor")]})
    assert v.verdict == "PASS"
    assert v.blocking == [] and len(v.minor) == 1


def test_ungrounded_findings_dropped_as_noise() -> None:
    v = Verdict.model_validate(
        {
            "verdict": "FAIL",
            "findings": [
                finding("F1", file=None, line=None, criterion=None),
                finding("F2", file="a.py", line=None, criterion=None),  # file without line
                finding("F3", file=None, line=None, criterion="AC1"),
                finding("F4", file="a.py", line=3, criterion=None),
            ],
        }
    )
    assert [f.id for f in v.findings] == ["F3", "F4"]
    assert [f.id for f in v.noise] == ["F1", "F2"]
    assert "noise" not in v.model_dump()


def test_fail_with_only_noise_keeps_fail_but_no_blocking() -> None:
    v = Verdict.model_validate(
        {"verdict": "FAIL", "findings": [finding(file=None, line=None, criterion=None)]}
    )
    assert v.blocking == []


def test_match_key() -> None:
    a = Finding.model_validate(finding(line=1))
    b = Finding.model_validate(finding(fid="F9", line=40))
    assert a.match_key() == b.match_key() == ("mathutils/core.py", "AC2")
    assert Finding.model_validate(finding(file=None, criterion=None)).match_key() is None


def test_author_response_disputed() -> None:
    r = AuthorResponse.model_validate(
        {
            "responses": [
                {"finding_id": "F1", "action": "fixed", "note": "ok"},
                {"finding_id": "F2", "action": "disputed", "reason": "AC3"},
            ]
        }
    )
    assert list(r.disputed()) == ["F2"]
