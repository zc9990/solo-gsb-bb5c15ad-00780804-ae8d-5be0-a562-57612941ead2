"""Tests for the address coverage-gap page (``/api/coverage-gaps``)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _no_match(client: TestClient, query: str, *, save: bool = True) -> dict:
    r = client.post("/api/match", json={"query": query, "save": save})
    data = r.json()
    assert data["status"] == "no_match", data
    return data


def _find_gap(client: TestClient, house: str) -> dict:
    r = client.get("/api/coverage-gaps")
    assert r.status_code == 200
    gaps = [g for g in r.json()["gaps"] if g["house"] == house]
    assert len(gaps) == 1, gaps
    return gaps[0]


def test_gap_structure_and_bad_filter(client):
    r = client.get("/api/coverage-gaps")
    assert r.status_code == 200
    data = r.json()
    for key in ("total", "open", "covered", "state", "gaps"):
        assert key in data
    assert data["state"] == "all"
    assert client.get("/api/coverage-gaps?state=nope").status_code == 400


def test_gap_groups_saved_no_match_logs_only(client):
    # Two spelling variants of one genuinely unknown address.
    q1 = "7771 Gapnova Street, Gapville GZ"
    q2 = "No. 7771 Gapnova St, Gapville GZ"
    log_1 = _no_match(client, q1)["match_id"]
    log_2 = _no_match(client, q2)["match_id"]

    # A no_match run that is not saved must not contribute.
    _no_match(client, q1, save=False)

    gap = _find_gap(client, "7771")
    assert gap["state"] == "open"
    assert gap["count"] == 2
    assert gap["street_tokens"] == ["gapnova", "st"]
    assert "gapville" in gap["locality_tokens"]
    # the representative query is the most recent occurrence
    assert gap["example_query"] == q2
    assert [m["query"] for m in gap["examples"]] == [q2, q1]
    assert gap["match_ids"] == sorted([log_1, log_2])
    assert gap["last_seen_at"] >= gap["first_seen_at"]
    # an open gap carries the current preferred candidate but no target
    assert gap["covered_target"] is None
    assert gap["current_status"] == "no_match"

    # needs_review / auto_matched logs never produce gaps
    r = client.post("/api/match", json={"query": "7772 Lake Dr, Austin TX"})
    assert r.json()["status"] == "needs_review"
    gaps = client.get("/api/coverage-gaps").json()["gaps"]
    assert not [g for g in gaps if g["house"] == "7772"]


def test_different_house_numbers_are_separate_gaps(client):
    _no_match(client, "7781 Gapnova Lane, Gapville GZ")
    _no_match(client, "7782 Gapnova Lane, Gapville GZ")
    r = client.get("/api/coverage-gaps")
    houses = {g["house"]: g["count"] for g in r.json()["gaps"]
              if g["street_tokens"] == ["gapnova", "ln"]}
    assert houses == {"7781": 1, "7782": 1}


def test_rerun_flips_gap_to_covered_after_upload_without_rewriting(client):
    # A near-miss address (same street, different house) leaves the gap open,
    # now reported against the current preferred candidate as needs_review.
    r = client.post("/api/addresses", json={"addresses": [{
        "address": "7770 Gapnova Street", "city": "Gapville", "state": "GZ",
        "external_id": "GAP-NEAR",
    }]})
    assert r.json()["inserted"] == 1
    gap = _find_gap(client, "7771")
    assert gap["state"] == "open"
    assert gap["current_status"] == "needs_review"
    assert gap["best_candidate"]["full_text"] == "7770 Gapnova Street"
    assert gap["covered_target"] is None

    # Uploading the actual address: the same old log group is now covered.
    r = client.post("/api/addresses", json={"addresses": [{
        "address": "7771 Gapnova Street", "city": "Gapville", "state": "GZ",
        "external_id": "GAP-EXACT",
    }]})
    assert r.json()["inserted"] == 1
    gap = _find_gap(client, "7771")
    assert gap["state"] == "covered"
    assert gap["current_status"] == "auto_matched"
    assert gap["covered_target"]["full_text"] == "7771 Gapnova Street"

    # filters honor the live re-evaluation
    open_gaps = client.get("/api/coverage-gaps?state=open").json()["gaps"]
    covered = client.get("/api/coverage-gaps?state=covered").json()["gaps"]
    assert not [g for g in open_gaps if g["house"] == "7771"]
    assert [g for g in covered if g["house"] == "7771"]

    # the original logs are never rewritten: they remain no_match
    logs = client.get("/api/matches?status=no_match&limit=500").json()["items"]
    records = [m for m in logs if m["id"] in gap["match_ids"]]
    assert len(records) == 2
    assert all(m["status"] == "no_match" for m in records)


def test_open_gap_rerun_candidates_are_renderable(client):
    # The current verdict embeds fresh candidates scored against the live table.
    gap = _find_gap(client, "7781")
    assert gap["state"] == "open"
    current = gap["current"]
    assert current["status"] in {"no_match", "needs_review"}
    for cand in current["candidates"]:
        assert {"id", "full_text"} <= set(cand["address"])
        assert "score" in cand
        assert {"matched", "query_only", "candidate_only"} <= set(cand["street_tokens"])
