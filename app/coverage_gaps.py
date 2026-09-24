"""地址覆盖缺口 (address coverage gaps).

Saved match logs whose scored verdict was ``no_match`` are aggregated by the
same normalised identity used by the review queue and the consensus memory:
sorted road (street) tokens, sorted locality tokens and the house number.
One identity is exactly one gap -- queries whose house numbers differ always
stay in separate buckets.

Whenever the gap list is built, every group's representative query is scored
**again** against the *current* standard address table with the *default* rule
settings (the live matcher, no consensus memory, no experiments).  A rerun
that now reaches ``auto_matched`` marks the gap as ``covered`` and records the
target address; every other verdict (still ``no_match`` or now
``needs_review``) leaves the gap ``open`` and carries the current preferred
candidate.  This is what keeps an old rejection record from looking
unresolved after the missing standard address has been uploaded.

Building the list is a pure read: it never inserts a log, rewrites an old
record or changes any scoring rule.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from . import db
from .config import Settings
from .consensus import norm_key
from .matcher import match_address

# Newest-first member rows exposed as original-query examples per gap.
EXAMPLE_LIMIT = 3


def _group_key(nkey: dict[str, Any]) -> str:
    """Stable digest of a normalised road/locality/house identity."""
    payload = json.dumps(nkey, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_dict(c: Any) -> dict[str, Any]:
    """Compact view of a fresh candidate for reuse by the front-end renderer."""
    return {
        "address": c.address,
        "score": c.score,
        "token_f1": c.token_score.f1,
        "street_f1": c.token_score.street.f1,
        "locality_f1": c.token_score.locality.f1,
        "house_score": c.house_score,
        "matched_tokens": (
            c.token_score.street.matched + c.token_score.locality.matched
        ),
        "conflicts": c.conflicts,
        "street_tokens": {
            "matched": c.token_score.street.matched,
            "query_only": c.token_score.street.query_only,
            "candidate_only": c.token_score.street.candidate_only,
        },
        "locality_tokens": {
            "matched": c.token_score.locality.matched,
            "query_only": c.token_score.locality.query_only,
            "candidate_only": c.token_score.locality.candidate_only,
        },
    }


def _current_verdict(result: Any) -> dict[str, Any]:
    """The parts of a fresh matcher run the gap page needs (scoring only)."""
    best = result.best
    return {
        "status": result.status,
        "reason": result.reason,
        "best_id": best.address["id"] if best else None,
        "best_score": best.score if best else None,
        "runner_up_id": result.runner_up.address["id"] if result.runner_up else None,
        "runner_up_score": result.runner_up.score if result.runner_up else None,
        "candidates": [_candidate_dict(c) for c in result.candidates],
    }


def compute_gaps(conn: sqlite3.Connection, settings: Settings) -> list[dict[str, Any]]:
    """Build the coverage-gap list from saved ``no_match`` logs.

    Logs are read newest first so each bucket's representative query is its
    most recent occurrence; every representative is then rerun against the
    live address table with ``settings`` (the default rules).
    """
    rows = conn.execute(
        "SELECT * FROM match_logs WHERE status = 'no_match' ORDER BY id DESC"
    ).fetchall()

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        nkey = norm_key(row["query"])
        key = _group_key(nkey)
        member = {
            "id": row["id"],
            "query": row["query"],
            "created_at": row["created_at"],
        }
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "group_key": key,
                "street_tokens": nkey["street"],
                "locality_tokens": nkey["locality"],
                "house": nkey["house"] or None,
                "members": [member],
            }
        else:
            bucket["members"].append(member)

    # The address table is read once; every group is scored against this same
    # current snapshot with the default rules.
    address_rows = db.all_addresses(conn)

    gaps: list[dict[str, Any]] = []
    for bucket in buckets.values():
        members = bucket["members"]  # newest first
        representative = members[0]["query"]
        result = match_address(representative, address_rows, settings)
        covered = result.status == "auto_matched"
        best = result.best
        match_ids = sorted(m["id"] for m in members)
        gaps.append({
            "group_key": bucket["group_key"],
            "street_tokens": bucket["street_tokens"],
            "locality_tokens": bucket["locality_tokens"],
            "house": bucket["house"],
            "count": len(members),
            "example_query": representative,
            "examples": members[:EXAMPLE_LIMIT],
            "match_ids": match_ids,
            "first_seen_at": members[-1]["created_at"],
            "last_seen_at": members[0]["created_at"],
            "state": "covered" if covered else "open",
            "current_status": result.status,
            "current_reason": result.reason,
            "best_candidate": (
                {"id": best.address["id"], "full_text": best.address["full_text"]}
                if best else None
            ),
            "covered_target": (
                {"id": best.address["id"], "full_text": best.address["full_text"]}
                if covered else None
            ),
            "current": _current_verdict(result),
        })

    # Open gaps first (the work list), then the most frequent and most recent
    # (a larger latest log id means a more recent occurrence).
    gaps.sort(key=lambda g: (
        0 if g["state"] == "open" else 1,
        -g["count"],
        -g["match_ids"][-1],
    ))
    return gaps
