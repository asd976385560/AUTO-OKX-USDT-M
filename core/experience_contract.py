# -*- coding: utf-8 -*-
"""Deterministic contract for decision-time historical-experience evidence.

The similarity tool owns the numbers.  Agents may explain how they use those
numbers, but must not recount truncated sample arrays or relabel cross-symbol
analogues as direct evidence.  This small contract lets writers verify that a
decision card copied one coherent, cycle-scoped snapshot.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


# v2（2026-08-10 Wave1 序8）：summaries 携带 sample_ids（trade_experiences 行 id，
# len==n 去重校验），计数与样本身份都由工具产出、hash 冻结——HYPE 事故里
# "reason 写 direct n=0 而卡内自带同标的亏损样本"的口径漂移自此无法手写。
PROTOCOL = "experience_evidence_v2"
SUMMARY_SCOPES = {
    "exact_setup": "same_symbol_side_action_regime",
    "same_symbol_similar": "same_symbol_similar",
    "cross_symbol_similar": "cross_symbol_similar",
}


def normalize_symbol(value: Any) -> str:
    """Return the canonical ``<BASE>-USDT-SWAP`` instrument id."""
    symbol = str(value or "").strip().upper()
    if not symbol:
        return ""
    if symbol.endswith("-USDT-SWAP"):
        return symbol
    if symbol.endswith("-USDT"):
        return symbol + "-SWAP"
    return symbol + "-USDT-SWAP"


def normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _hash_payload(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_contract(
    query: dict[str, Any],
    *,
    exact_setup: dict[str, Any],
    same_symbol_similar: dict[str, Any],
    cross_symbol_similar: dict[str, Any],
    samples_truncated: bool = True,
) -> dict[str, Any]:
    """Build a signed-by-content evidence snapshot.

    The hash is not an authenticity signature.  It is an integrity check that
    prevents an agent from copying a query from one tool call and counts from
    another, or from silently editing the canonical counts in prose.
    """
    summaries = {
        "exact_setup": dict(exact_setup),
        "same_symbol_similar": dict(same_symbol_similar),
        "cross_symbol_similar": dict(cross_symbol_similar),
    }
    # v2：sample_ids 缺省补 []（n=0 的空 scope 合法；n>0 缺 ids 由 validate 拒）。
    for summary in summaries.values():
        summary.setdefault("sample_ids", [])
    body = {
        "protocol": PROTOCOL,
        "query": dict(query),
        "summaries": summaries,
        "samples_truncated": bool(samples_truncated),
    }
    body["evidence_hash"] = _hash_payload(body)
    return body


def validate_contract(
    contract: Any,
    *,
    expected_symbol: str | None = None,
    expected_side: str | None = None,
    expected_regime: str | None = None,
    expected_action: str | None = None,
    expected_profile: str | None = None,
    expected_as_of: str | None = None,
) -> list[str]:
    """Validate integrity, count arithmetic and optional decision identity."""
    if not isinstance(contract, dict):
        return ["evidence_contract 必须是 dict"]
    errors: list[str] = []
    if contract.get("protocol") != PROTOCOL:
        errors.append(f"protocol 必须是 {PROTOCOL}")

    query = contract.get("query")
    if not isinstance(query, dict):
        errors.append("query 必须是 dict")
        query = {}
    expected = {
        "symbol": normalize_symbol(expected_symbol)
        if expected_symbol is not None else None,
        "side": normalize_token(expected_side)
        if expected_side is not None else None,
        "regime": normalize_token(expected_regime)
        if expected_regime is not None else None,
        "action": normalize_token(expected_action)
        if expected_action is not None else None,
        "profile": normalize_token(expected_profile)
        if expected_profile is not None else None,
        "as_of": str(expected_as_of).strip()
        if expected_as_of is not None else None,
    }
    actual = {
        "symbol": normalize_symbol(query.get("symbol")),
        "side": normalize_token(query.get("side")),
        "regime": normalize_token(query.get("regime")),
        "action": normalize_token(query.get("action")),
        "profile": normalize_token(query.get("profile")),
        "as_of": str(query.get("as_of") or "").strip(),
    }
    for key, expected_value in expected.items():
        if expected_value is not None and actual[key] != expected_value:
            errors.append(
                f"query.{key} 与决策不一致: {actual[key]!r}!={expected_value!r}"
            )

    summaries = contract.get("summaries")
    if not isinstance(summaries, dict):
        errors.append("summaries 必须是 dict")
        summaries = {}
    for key, scope in SUMMARY_SCOPES.items():
        summary = summaries.get(key)
        if not isinstance(summary, dict):
            errors.append(f"summaries.{key} 必须是 dict")
            continue
        if summary.get("scope") != scope:
            errors.append(f"summaries.{key}.scope 必须是 {scope}")
        counts: dict[str, int] = {}
        for field in ("n", "wins", "losses"):
            value = summary.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"summaries.{key}.{field} 必须是非负整数")
            else:
                counts[field] = value
        if set(counts) == {"n", "wins", "losses"}:
            if counts["n"] != counts["wins"] + counts["losses"]:
                errors.append(
                    f"summaries.{key} 计数不自洽: "
                    f"n={counts['n']} != wins+losses="
                    f"{counts['wins'] + counts['losses']}"
                )
        sample_ids = summary.get("sample_ids")
        if not isinstance(sample_ids, list) or any(
                not isinstance(x, int) or isinstance(x, bool)
                for x in sample_ids):
            errors.append(
                f"summaries.{key}.sample_ids 必须是整数 id 列表（v2 契约）")
        else:
            if len(set(sample_ids)) != len(sample_ids):
                errors.append(f"summaries.{key}.sample_ids 含重复 id")
            if "n" in counts and len(sample_ids) != counts["n"]:
                errors.append(
                    f"summaries.{key}.sample_ids 数量 {len(sample_ids)} "
                    f"!= n={counts['n']}（计数与样本身份必须同源）")

    supplied_hash = contract.get("evidence_hash")
    unsigned = {key: value for key, value in contract.items()
                if key != "evidence_hash"}
    expected_hash = _hash_payload(unsigned)
    if supplied_hash != expected_hash:
        errors.append("evidence_hash 校验失败（查询与摘要可能来自不同工具输出）")
    return errors
