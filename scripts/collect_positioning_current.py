# -*- coding: utf-8 -*-
"""采集当前自然周期的全宇宙官方 1H 账户多空比。

该入口位于冻结研究模型的代码清单之外，供 ``fast_collect.py`` 在自然
``:00/:30`` 周期调用。首次全量请求结束后，只对本次仍无效的精确集合做最多
两波有界恢复；每一波只接收上一波尚未恢复的标的。不接受历史周期，不回填、
不改变分析/风控/下单逻辑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _okx_http
from _db_ro import connect_ro
from collect_market_features import (
    DEFAULT_POSITIONING_SYMBOLS,
    POSITIONING_MAXIMUM_SOURCE_AGE_S,
    POSITIONING_MINIMUM_COVERAGE,
    positioning_batch_passed,
    positioning_source_freshness,
    rest_positioning_row,
    select_positioning_symbols,
    utc_now_iso,
    write_positioning_rows,
)


CST = timezone(timedelta(hours=8))
ROOT = Path(r".")
INITIAL_TIMEOUT_SECONDS = 30.0
INITIAL_WORKERS = 12
RETRY_TIMEOUT_SECONDS = 12.0
RETRY_WORKERS = 12
MAX_RETRY_WAVES = 2
INITIAL_REQUEST_RETRIES = 1
RETRY_CONTRACT_VERSION = 2
_VALIDATION_TIMESTAMP = "1970-01-01T00:00:00Z"


def positioning_receipt_path(db_root: Path, cycle_id: str) -> Path:
    """生产收据进质量目录；隔离库收据留在隔离根旁。"""
    try:
        production = db_root.resolve() == (ROOT / "db").resolve()
    except OSError:
        production = False
    base = (
        ROOT / "reports" / "quality" / "positioning-current"
        if production
        else db_root.parent / "reports" / "quality" / "positioning-current"
    )
    slug = cycle_id.replace("-", "").replace(":", "")
    return base / cycle_id[:10] / f"positioning-{slug}.json"


def write_new_json(path: Path, payload: dict) -> dict[str, object]:
    """以同目录完整临时文件 + 新硬链接原子发布，不覆盖既有周期收据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"immutable positioning receipt exists: {path}")
    data = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2,
    ) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # link 在目标已存在时失败，避免 os.replace 的静默覆盖语义。
        os.link(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def require_current_natural_cycle(
    cycle_id: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """只允许当前自然 ``:00/:30`` 槽，明确拒绝历史补采。"""
    try:
        parsed = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError("cycle must use YYYY-MM-DDTHH:MM") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M") != cycle_id:
        raise ValueError("cycle must use canonical YYYY-MM-DDTHH:MM")
    parsed = parsed.replace(tzinfo=CST)
    if parsed.minute not in (0, 30):
        raise ValueError("positioning cycle must be a natural :00/:30 slot")

    current = (now or datetime.now(CST)).astimezone(CST)
    current_slot = current.replace(
        minute=(current.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    if parsed != current_slot:
        raise ValueError(
            "historical/future positioning collection is forbidden; "
            f"requested={cycle_id} current_slot="
            f"{current_slot.strftime('%Y-%m-%dT%H:%M')}"
        )
    return parsed


def _payload_valid(payload, cycle_id: str, symbol: str) -> bool:
    try:
        rest_positioning_row(
            payload or [], cycle_id, _VALIDATION_TIMESTAMP, symbol)
        return True
    except Exception:  # noqa: BLE001 - 与正式逐币隔离解析保持一致
        return False


def _retry_failed_payloads(
    symbols: list[str],
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    """对精确失败集合发一次请求；无内部重试且有共享截止时间。"""
    if not symbols:
        return {}
    path = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
    return _okx_http._batch(
        symbols,
        lambda _symbol: path,
        lambda symbol: {
            "instId": symbol,
            "period": "1H",
            "limit": "1",
        },
        lambda data: data,
        batch_timeout_s=RETRY_TIMEOUT_SECONDS,
        workers=RETRY_WORKERS,
        throttle_key_fn=lambda symbol: symbol,
        request_retries=0,
        outcomes=outcomes,
    )


def _fetch_initial_payloads(
    symbols: list[str],
    *,
    outcomes: dict[str, dict] | None = None,
) -> dict:
    """低并发首批；避免 48 路 TLS 峰值把大批请求拖到共享截止时间。"""
    if not symbols:
        return {}
    path = "/api/v5/rubik/stat/contracts/long-short-account-ratio-contract"
    return _okx_http._batch(
        symbols,
        lambda _symbol: path,
        lambda symbol: {
            "instId": symbol,
            "period": "1H",
            "limit": "1",
        },
        lambda data: data,
        batch_timeout_s=INITIAL_TIMEOUT_SECONDS,
        workers=INITIAL_WORKERS,
        throttle_key_fn=lambda symbol: symbol,
        request_retries=INITIAL_REQUEST_RETRIES,
        outcomes=outcomes,
    )


def fetch_positioning_rows_bounded(
    symbols: list[str],
    cycle_id: str,
) -> tuple[list[tuple], list[str], dict[str, object]]:
    """首次全量 + 最多两波递减精确恢复，最终统一 availability。"""
    initial_outcomes: dict[str, dict] = {}
    initial_batch_error: str | None = None
    try:
        payloads = _fetch_initial_payloads(
            symbols,
            outcomes=initial_outcomes,
        )
    except Exception as exc:  # noqa: BLE001 - 系统性首批失败仍只有两波有界补救
        payloads = {}
        initial_batch_error = f"{type(exc).__name__}: {exc}"[:500]

    initial_valid = [
        symbol for symbol in symbols
        if _payload_valid(payloads.get(symbol), cycle_id, symbol)
    ]
    initial_valid_set = set(initial_valid)
    retry_symbols = [symbol for symbol in symbols if symbol not in initial_valid_set]
    remaining = list(retry_symbols)
    recovered: list[str] = []
    retry_waves: list[dict[str, object]] = []
    retry_transport_failures = 0
    retry_error_types: Counter[str] = Counter()
    for wave_number in range(1, MAX_RETRY_WAVES + 1):
        if not remaining:
            break
        requested = list(remaining)
        wave_outcomes: dict[str, dict] = {}
        wave_batch_error: str | None = None
        try:
            retry_payloads = _retry_failed_payloads(
                requested,
                outcomes=wave_outcomes,
            )
        except Exception as exc:  # noqa: BLE001 - 下一有界波仍可恢复
            retry_payloads = {}
            wave_batch_error = f"{type(exc).__name__}: {exc}"[:500]

        wave_recovered: list[str] = []
        for symbol in requested:
            candidate = retry_payloads.get(symbol)
            if _payload_valid(candidate, cycle_id, symbol):
                payloads[symbol] = candidate
                wave_recovered.append(symbol)
        recovered_set = set(wave_recovered)
        remaining = [
            symbol for symbol in requested if symbol not in recovered_set
        ]
        wave_failures = sum(
            1 for outcome in wave_outcomes.values()
            if not bool(outcome.get("ok"))
        )
        wave_errors = Counter(
            str(outcome.get("error_type") or "unknown")
            for outcome in wave_outcomes.values()
            if not bool(outcome.get("ok"))
        )
        if wave_batch_error:
            wave_errors["batch_error"] += len(requested)
            wave_failures = max(wave_failures, len(requested))
        retry_transport_failures += wave_failures
        retry_error_types.update(wave_errors)
        recovered.extend(wave_recovered)
        retry_waves.append({
            "wave": wave_number,
            "requested_symbols": len(requested),
            "requested_symbol_values": requested,
            "recovered_symbols": len(wave_recovered),
            "recovered_symbol_values": wave_recovered,
            "remaining_symbols": len(remaining),
            "remaining_symbol_values": list(remaining),
            "transport_failures": wave_failures,
            "error_types": dict(sorted(wave_errors.items())),
            "batch_error": wave_batch_error,
            "timeout_seconds": RETRY_TIMEOUT_SECONDS,
            "workers": RETRY_WORKERS,
            "request_retries_per_symbol": 0,
        })

    # availability 是全批次（含有界重试）结束后时刻；所有行必须完全一致。
    available_at = utc_now_iso()
    rows: list[tuple] = []
    errors: list[str] = []
    for symbol in symbols:
        try:
            rows.append(rest_positioning_row(
                payloads.get(symbol) or [], cycle_id, available_at, symbol,
            ))
        except Exception as exc:  # noqa: BLE001 - 单币失败隔离并留在分母
            errors.append(
                f"{symbol}:positioning:{type(exc).__name__}:{exc}")
    rows.sort(key=lambda row: row[3])

    stats: dict[str, object] = {
        "initial_requested_symbols": len(symbols),
        "initial_valid_symbols": len(initial_valid),
        "initial_invalid_symbols": len(retry_symbols),
        "initial_invalid_symbol_values": retry_symbols,
        "initial_transport_failures": sum(
            1 for outcome in initial_outcomes.values()
            if not bool(outcome.get("ok"))
        ),
        "initial_batch_error": initial_batch_error,
        "initial_timeout_seconds": INITIAL_TIMEOUT_SECONDS,
        "initial_workers": INITIAL_WORKERS,
        "initial_request_retries_per_symbol": INITIAL_REQUEST_RETRIES,
        "retry_requested_symbols": len(retry_symbols),
        "retry_requested_symbol_values": retry_symbols,
        "retry_recovered_symbols": len(recovered),
        "retry_recovered_symbol_values": recovered,
        "retry_transport_failures": retry_transport_failures,
        "final_failed_symbols": len(errors),
        "final_failed_symbol_values": [
            error.split(":positioning:", 1)[0] for error in errors
        ],
        "initial_error_types": dict(sorted(Counter(
            str(outcome.get("error_type") or "unknown")
            for outcome in initial_outcomes.values()
            if not bool(outcome.get("ok"))
        ).items())),
        "retry_error_types": dict(sorted(retry_error_types.items())),
        "retry_timeout_seconds": RETRY_TIMEOUT_SECONDS,
        "retry_workers": RETRY_WORKERS,
        "retry_contract_version": RETRY_CONTRACT_VERSION,
        "retry_wave_count": len(retry_waves),
        "retry_waves": retry_waves,
        "retry_attempts_per_symbol": MAX_RETRY_WAVES,
        "retry_max_attempts_per_symbol": MAX_RETRY_WAVES,
        "maximum_network_budget_seconds": (
            INITIAL_TIMEOUT_SECONDS
            + RETRY_TIMEOUT_SECONDS * MAX_RETRY_WAVES
        ),
        "maximum_official_requests_per_symbol": (
            INITIAL_REQUEST_RETRIES + 1 + MAX_RETRY_WAVES
        ),
        "unbounded_retry": False,
        "historical_retry": False,
        "shared_available_at_utc": available_at,
    }
    return rows, errors, stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="当前自然周期全宇宙官方1H账户多空比采集")
    parser.add_argument("--db-root", default=r".\db")
    parser.add_argument("--cycle", required=True)
    parser.add_argument(
        "--positioning-max-symbols",
        type=int,
        default=DEFAULT_POSITIONING_SYMBOLS,
    )
    args = parser.parse_args()

    receipt_path: Path | None = None
    try:
        require_current_natural_cycle(args.cycle)
        db_root = Path(args.db_root)
        db_path = db_root / "market.db"
        receipt_path = positioning_receipt_path(db_root, args.cycle)
        limit = max(3, min(int(args.positioning_max_symbols), 1000))
        read_connection = connect_ro(db_path, timeout=20)
        try:
            symbols = select_positioning_symbols(read_connection, limit)
        finally:
            read_connection.close()
        if not symbols:
            raise RuntimeError("current positioning universe is empty")

        rows, errors, retry_stats = fetch_positioning_rows_bounded(
            symbols, args.cycle)
        connection = sqlite3.connect(str(db_path), timeout=20)
        try:
            wrote = write_positioning_rows(connection, rows)
            connection.commit()
        finally:
            connection.close()

        selected_count = len(symbols)
        coverage_rate = wrote / selected_count if selected_count else 0.0
        coverage_passed = positioning_batch_passed(
            selected_count=selected_count,
            positioning_rows=wrote,
        )
        freshness = positioning_source_freshness(rows)
        quality_passed = coverage_passed and bool(freshness["passed"])
        warnings = []
        if retry_stats["initial_batch_error"]:
            warnings.append(
                f"initial_batch:{retry_stats['initial_batch_error']}")
        selected_json = json.dumps(
            symbols, ensure_ascii=False, separators=(",", ":"))
        result = {
            "ok": quality_passed,
            "degraded": not quality_passed,
            "cycle": args.cycle,
            "selected": symbols,
            "selected_count": selected_count,
            "selected_symbols_sha256": hashlib.sha256(
                selected_json.encode("utf-8")).hexdigest(),
            "wrote": {"positioning": wrote},
            "minimum_positioning_coverage": POSITIONING_MINIMUM_COVERAGE,
            "positioning_coverage_rate": coverage_rate,
            "maximum_positioning_source_age_minutes": (
                POSITIONING_MAXIMUM_SOURCE_AGE_S / 60.0),
            "positioning_source_freshness": freshness,
            "positioning_due": True,
            "positioning_only": True,
            "positioning_transport": (
                "okx_official_rest_bounded_exact_retry_waves_v2"),
            "natural_current_cycle_guard": True,
            "historical_backfill_allowed": False,
            "retry": retry_stats,
            "warnings": warnings,
            "errors": errors[:20],
        }
        receipt_payload = {
            "schema_version": 1,
            "artifact_type": "current_natural_positioning_collection_receipt",
            "status": "PASSED" if quality_passed else "NOT_MET",
            "generated_at_utc": utc_now_iso(),
            **result,
            "safety": {
                "natural_current_cycle_only": True,
                "historical_backfill_allowed": False,
                "production_model_mutation": False,
                "production_threshold_mutation": False,
                "orders_placed": 0,
            },
        }
        receipt = write_new_json(receipt_path, receipt_payload)
        result["receipt"] = receipt
        print(json.dumps(result, ensure_ascii=False))
        return 0 if quality_passed else 1
    except Exception as exc:  # noqa: BLE001 - 单行机器可读失败收据
        failure = {
            "ok": False,
            "degraded": True,
            "cycle": args.cycle,
            "historical_backfill_allowed": False,
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }
        if receipt_path is not None and not receipt_path.exists():
            try:
                failure["receipt"] = write_new_json(receipt_path, {
                    "schema_version": 1,
                    "artifact_type": (
                        "current_natural_positioning_collection_receipt"),
                    "status": "FAILED",
                    "generated_at_utc": utc_now_iso(),
                    **failure,
                    "safety": {
                        "natural_current_cycle_only": True,
                        "historical_backfill_allowed": False,
                        "production_model_mutation": False,
                        "production_threshold_mutation": False,
                        "orders_placed": 0,
                    },
                })
            except Exception as receipt_exc:  # noqa: BLE001
                failure["receipt_error"] = (
                    f"{type(receipt_exc).__name__}: {receipt_exc}"[:500])
        print(json.dumps(failure, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
