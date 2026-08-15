# -*- coding: utf-8 -*-
"""V2.0 §7 —— okx swap 命令封装（开/平/algo/fills/leverage/行情）。

把分散在 LLM 现场拼的 `okx --profile <p> swap …` 命令收进本模块，统一从
`_okxcli.okx_json` 调（复用节流/超时/崩溃重试）。order_executor 只调本模块函数，
不再手拼命令字符串。

**命令已在 OKX CLI 1.4.2 复核兼容**（初测 2026-06-24，2026-07-27 升级后复核）：
  - place: okx swap place --instId <id> --side <buy|sell> --ordType <market|limit> --sz <n>
           [--posSide long|short] [--tdMode cross|isolated] [--tgtCcy base_ccy|quote_ccy|margin]
           [--reduceOnly] [--slTriggerPx <px>] [--slOrdPx <px|-1>] [--slTriggerPxType last|index|mark]
           [--tpTriggerPx <px>] [--tpOrdPx <px|-1>] [--tpTriggerPxType last|index|mark]
           （单腿兼容参数；生产 executor 不同时传 TP+SL，避免普通 conditional 忽略 TP）
  - algo place: okx swap algo place --instId --side --sz --ordType conditional
           --slTriggerPx <px> --slOrdPx -1 --slTriggerPxType mark [--posSide] [--tdMode] [--reduceOnly]
           （同形支持单腿 TP；TP+SL 双腿必须用 ordType=oco，不能用 conditional）
  - algo amend: okx swap algo amend --instId --algoId [--newSz <n>]
           [--newSlTriggerPx <px>] [--newSlOrdPx <px|-1>] [--newTpTriggerPx <px>] [--newTpOrdPx <px|-1>]
           （2026-08-13 主人核实 CLI 1.4.2 help；**含 attached TP/SL**，服务端原子改单——
            移动止损/加仓后扩全仓的主路径，无「零张/两张止损」中间窗口）
  - algo cancel: okx swap algo cancel --instId --algoId
  - close: okx swap close --instId <id> --mgnMode <cross|isolated> [--posSide net|long|short]
  - fills: okx swap fills [--instId <id>] [--ordId <id>] [--archive]
  - leverage: okx swap leverage --instId --lever --mgnMode <cross|isolated> [--posSide]
  - positions: okx swap positions [<instId>]
  - market mark-price --instType SWAP [--instId]; market instruments --instType SWAP [--instId]

**DRYRUN**（OKX_EXECUTOR_DRYRUN=1）：变更类命令（leverage/place/close/algo cancel）不真发，
返回 simulated 回执；只读命令（positions/fills/mark-price/instruments）仍真跑（安全）。
单测一律 monkeypatch 本模块函数，不依赖网络。

零模型名（红线 #1）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Optional

# 复用 scripts/_okxcli（CLI 调用 + 节流 + 崩溃重试）
_SCRIPTS = os.environ.get("OKX_SCRIPTS_DIR", r".\scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from _okxcli import okx_json  # noqa: E402


def is_dryrun() -> bool:
    return os.environ.get("OKX_EXECUTOR_DRYRUN", "") in ("1", "true", "True")


def _global_args(profile: str) -> list[str]:
    return ["--profile", profile]


# ---------------------------------------------------------------------------
# 结果归一：okx_json 成功返回 payload；失败抛 RuntimeError/TimeoutError。
# 统一成 {ok, sCode, sMsg, data, error}，order_executor 据此分支（永不让异常穿透）。
# ---------------------------------------------------------------------------
# 业务 sCode（§7）：平仓/下单时需识别的关键码
SCODE_DELISTED = "51087"                 # 标的下架
SCODE_NOT_EXIST = "51001"                # 标的不存在


def _extract_scode(text: str) -> Optional[str]:
    """从 CLI 错误文本里抠出 5xxxx sCode（OKX 错误码）。"""
    import re
    m = re.search(r"\b(5\d{4})\b", text or "")
    return m.group(1) if m else None


def _normalize(payload: Any) -> dict[str, Any]:
    """把 okx_json 返回的多形态 payload 归一。

    常见形态：
      - {"code":"0","msg":"","data":[{...}]}（OKX API 原样）
      - {"data":[{"sCode":"0","ordId":"...","sMsg":""}]}
      - [ {...}, ... ]（裸列表）
    """
    out: dict[str, Any] = {"ok": True, "sCode": "0", "sMsg": "", "data": [], "raw": payload}
    if isinstance(payload, dict):
        code = str(payload.get("code", "0"))
        data = payload.get("data")
        if isinstance(data, list):
            out["data"] = data
        elif data is not None:
            out["data"] = [data]
        # 顶层 code 非 0 = 整单失败
        if code not in ("0", ""):
            out["ok"] = False
            out["sCode"] = code
            out["sMsg"] = str(payload.get("msg", ""))
        # 行级 sCode（place/close 多在 data[0].sCode）
        if out["data"]:
            row0 = out["data"][0]
            if isinstance(row0, dict):
                rc = str(row0.get("sCode", "0"))
                if rc not in ("0", ""):
                    out["ok"] = False
                    out["sCode"] = rc
                    out["sMsg"] = str(row0.get("sMsg", "")) or out["sMsg"]
    elif isinstance(payload, list):
        out["data"] = payload
    return out


def _call(*args: str, profile: str, timeout_sec: float = 45.0) -> dict[str, Any]:
    """调 okx_json + 归一；异常不穿透，落进 {ok:False, error, sCode}。"""
    try:
        payload = okx_json(*args, global_args=_global_args(profile), timeout_sec=timeout_sec)
    except Exception as exc:  # RuntimeError/TimeoutError/FileNotFound
        msg = str(exc)
        return {"ok": False, "sCode": _extract_scode(msg), "sMsg": msg,
                "data": [], "raw": None, "error": msg}
    return _normalize(payload)


# ---------------------------------------------------------------------------
# 只读（DRYRUN 也真跑——安全）
# ---------------------------------------------------------------------------
def get_positions(profile: str, inst_id: Optional[str] = None) -> dict[str, Any]:
    args = ["account", "positions", "--instType", "SWAP"]
    return _call(*args, profile=profile)


def get_balance(profile: str) -> dict[str, Any]:
    return _call("account", "balance", profile=profile)


def get_max_size(inst_id: str, td_mode: str, profile: str) -> dict[str, Any]:
    """查询交易所按当前账户状态计算的最大可下单张数。

    ``account max-size`` 是只读接口；对于 SWAP，回包 ``maxBuy/maxSell`` 与
    下单参数 ``sz`` 同为合约张数。接口默认按账户当前杠杆计算，因此调用方若要
    使用目标杠杆，必须先确认/设置该杠杆，再调用本函数，不能用余额公式回推。
    """
    return _call(
        "account", "max-size",
        "--instId", inst_id,
        "--tdMode", td_mode,
        profile=profile,
    )


def get_mark_price(inst_id: str, profile: str) -> Optional[float]:
    r = _call("market", "mark-price", "--instType", "SWAP", "--instId", inst_id,
              profile=profile)
    for row in r.get("data", []):
        if isinstance(row, dict) and row.get("markPx"):
            try:
                return float(row["markPx"])
            except (TypeError, ValueError):
                return None
    return None


def get_instrument(inst_id: str, profile: str) -> Optional[dict[str, Any]]:
    """现拉 instrument（ctVal/lotSz/minSz）——instruments_cache stale/缺时兜底。"""
    r = _call("market", "instruments", "--instType", "SWAP", "--instId", inst_id,
              profile=profile)
    for row in r.get("data", []):
        if isinstance(row, dict) and row.get("instId") == inst_id:
            return row
    return None


def get_fills(inst_id: str, profile: str, ord_id: Optional[str] = None,
              archive: bool = False) -> list[dict[str, Any]]:
    args = ["swap", "fills", "--instId", inst_id]
    if ord_id:
        args += ["--ordId", ord_id]
    if archive:
        args.append("--archive")
    r = _call(*args, profile=profile)
    return [row for row in r.get("data", []) if isinstance(row, dict)]


def get_order(inst_id: str, ord_id: str, profile: str) -> Optional[dict[str, Any]]:
    """按 ordId 查单条订单状态（swap get → GET /trade/order）。

    即时端点：demo fills 端点延迟 6-52s 时订单状态已 filled（2026-07-03 实测
    uTime−cTime=134ms），含 state/avgPx/accFillSz/pnl —— 作 fills 的第二权威源。"""
    r = _call("swap", "get", "--instId", inst_id, "--ordId", str(ord_id),
              profile=profile)
    for row in r.get("data", []):
        if isinstance(row, dict) and str(row.get("ordId")) == str(ord_id):
            return row
    return None


def get_orders_history(inst_id: str, profile: str,
                       archive: bool = False) -> list[dict[str, Any]]:
    """近 7 天（archive=3 月）订单列表——close 无 ordId 时按时间窗反查平仓单
    （`swap close` 产生的单以独立 ordId + reduceOnly=true 出现在此，字段完整）。"""
    args = ["swap", "orders", "--instId", inst_id, "--history"]
    if archive:
        args.append("--archive")
    r = _call(*args, profile=profile)
    return [row for row in r.get("data", []) if isinstance(row, dict)]


def get_algo_orders(inst_id: str, profile: str, ord_type: Optional[str] = None,
                    history: bool = False) -> list[dict[str, Any]]:
    """列该 instId 的 algo 单（pending 默认，`--history` 取历史）。**附挂 SL 以仓位关联
    条件单出现在此**（含 slTriggerPx/side/state/algoId）——用于开仓后回读确认 SL 真挂上
    （#5 2026-07-07）。ord_type=None 时不过滤（含 conditional/oco）。只读（DRYRUN 也真跑）。"""
    args = ["swap", "algo", "orders", "--instId", inst_id]
    if ord_type:
        args += ["--ordType", ord_type]
    if history:
        args.append("--history")
    r = _call(*args, profile=profile)
    return [row for row in r.get("data", []) if isinstance(row, dict)]


# ---------------------------------------------------------------------------
# 变更（DRYRUN 短路）
# ---------------------------------------------------------------------------
def set_leverage(inst_id: str, lever: float, mgn_mode: str, profile: str,
                 pos_side: Optional[str] = None) -> dict[str, Any]:
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN", "data": [], "dryrun": True}
    args = ["swap", "leverage", "--instId", inst_id, "--lever", str(lever),
            "--mgnMode", mgn_mode]
    if pos_side and mgn_mode == "isolated":
        args += ["--posSide", pos_side]
    return _call(*args, profile=profile)


def place_market_open(inst_id: str, pos_side: str, sz: float, profile: str,
                      mgn_mode: str = "cross", tgt_ccy: str = "base_ccy",
                      sl_trigger_px: Optional[float] = None,
                      sl_trigger_px_type: str = "mark",
                      tp_trigger_px: Optional[float] = None,
                      tp_trigger_px_type: str = "mark") -> dict[str, Any]:
    """市价开仓（long→buy / short→sell），兼容单腿附挂 TP 或 SL。

    生产 executor 只在此附挂 SL；fixed TP 随后以独立 reduceOnly algo 创建。
    普通 conditional 同时给两腿时 OKX 会忽略 TP，调用方不得依赖该组合。
    """
    side = "buy" if pos_side == "long" else "sell"
    if sl_trigger_px is not None and tp_trigger_px is not None:
        return {
            "ok": False, "sCode": None,
            "sMsg": "combined_tp_sl_unsupported_use_independent_tp",
            "data": [], "raw": None,
            "error": "combined_tp_sl_unsupported_use_independent_tp",
            "sl_attached": False, "tp_attached": False,
        }
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"ordId": "DRYRUN-OPEN", "sCode": "0"}], "dryrun": True,
                "sl_attached": sl_trigger_px is not None,
                "tp_attached": tp_trigger_px is not None}
    # 注：SWAP 不支持 --tgtCcy（sCode 59110，2026-07-02 修）——tgtCcy(base/quote_ccy 计量)
    # 是现货/杠杆概念，永续 sz 恒为合约张数。tgt_ccy 形参保留兼容签名但不再下发。
    args = ["swap", "place", "--instId", inst_id, "--side", side,
            "--ordType", "market", "--sz", str(sz), "--posSide", pos_side,
            "--tdMode", mgn_mode]
    if sl_trigger_px is not None:
        # `--slOrdPx=-1`（等号形式，2026-07-02 修）：值 -1(市价止损)以短横开头，
        # 空格分隔会被 commander.js 当成另一个 flag → "argument is ambiguous" rc=1
        # → 带 SL 的 live/demo 下单一直失败（被 HOLD 掩盖）。等号形式才正确传值。
        args += ["--slTriggerPx", str(sl_trigger_px), "--slOrdPx=-1",
                 "--slTriggerPxType", sl_trigger_px_type]
    if tp_trigger_px is not None:
        args += ["--tpTriggerPx", str(tp_trigger_px), "--tpOrdPx=-1",
                 "--tpTriggerPxType", tp_trigger_px_type]
    r = _call(*args, profile=profile)
    r["sl_attached"] = (sl_trigger_px is not None) and r.get("ok", False)
    r["tp_attached"] = (tp_trigger_px is not None) and r.get("ok", False)
    return r


def place_algo_sl(inst_id: str, pos_side: str, sz: float, sl_trigger_px: float,
                  profile: str, mgn_mode: str = "cross",
                  sl_trigger_px_type: str = "mark") -> dict[str, Any]:
    """独立 reduceOnly 止损 algo（附挂失败的兜底）。平 long 用 sell、平 short 用 buy。"""
    close_side = "sell" if pos_side == "long" else "buy"
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"algoId": "DRYRUN-ALGO", "sCode": "0"}], "dryrun": True}
    args = ["swap", "algo", "place", "--instId", inst_id, "--side", close_side,
            "--sz", str(sz), "--ordType", "conditional",
            "--slTriggerPx", str(sl_trigger_px), "--slOrdPx=-1",  # 等号形式，见 place_market_open 注释
            "--slTriggerPxType", sl_trigger_px_type, "--posSide", pos_side,
            "--tdMode", mgn_mode, "--reduceOnly"]
    return _call(*args, profile=profile)


def place_algo_tp(inst_id: str, pos_side: str, sz: float, tp_trigger_px: float,
                  profile: str, mgn_mode: str = "cross",
                  tp_trigger_px_type: str = "mark") -> dict[str, Any]:
    """独立 reduceOnly 止盈 algo（与 place_algo_sl 同形，只换 tp 三参）。

    止盈只减仓不增仓，无风险预算影响；缺失不构成裸仓。
    """
    close_side = "sell" if pos_side == "long" else "buy"
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"algoId": "DRYRUN-ALGO-TP", "sCode": "0"}], "dryrun": True}
    args = ["swap", "algo", "place", "--instId", inst_id, "--side", close_side,
            "--sz", str(sz), "--ordType", "conditional",
            "--tpTriggerPx", str(tp_trigger_px), "--tpOrdPx=-1",  # 等号形式，见 place_market_open 注释
            "--tpTriggerPxType", tp_trigger_px_type, "--posSide", pos_side,
            "--tdMode", mgn_mode, "--reduceOnly"]
    return _call(*args, profile=profile)


def place_algo_protection(
    inst_id: str,
    pos_side: str,
    sz: float,
    sl_trigger_px: float,
    profile: str,
    *,
    tp_trigger_px: Optional[float] = None,
    mgn_mode: str = "cross",
    trigger_px_type: str = "mark",
) -> dict[str, Any]:
    """Place one reduce-only protection algo; use OCO when TP is requested."""
    close_side = "sell" if pos_side == "long" else "buy"
    if is_dryrun():
        return {
            "ok": True, "sCode": "0", "sMsg": "DRYRUN",
            "data": [{"algoId": "DRYRUN-ALGO-PROTECTION", "sCode": "0"}],
            "dryrun": True,
            "ord_type": "oco" if tp_trigger_px is not None else "conditional",
        }
    args = [
        "swap", "algo", "place", "--instId", inst_id, "--side", close_side,
        "--sz", str(sz), "--ordType", (
            "oco" if tp_trigger_px is not None else "conditional"),
        "--slTriggerPx", str(sl_trigger_px), "--slOrdPx=-1",
        "--slTriggerPxType", trigger_px_type,
        "--posSide", pos_side, "--tdMode", mgn_mode, "--reduceOnly",
    ]
    if tp_trigger_px is not None:
        args += [
            "--tpTriggerPx", str(tp_trigger_px), "--tpOrdPx=-1",
            "--tpTriggerPxType", trigger_px_type,
        ]
    return _call(*args, profile=profile)


def amend_algo_protection(inst_id: str, algo_id: str, profile: str,
                          new_sl_trigger_px: Optional[float] = None,
                          new_tp_trigger_px: Optional[float] = None,
                          new_sz: Optional[float] = None) -> dict[str, Any]:
    """原子改单（CLI 1.4.2 `swap algo amend`，含 attached TP/SL）。

    `okx swap algo amend --instId <id> --algoId <id> [--newSz <n>]
     [--newSlTriggerPx <px>] [--newSlOrdPx=-1] [--newTpTriggerPx <px>] [--newTpOrdPx=-1]`

    **这是移动止损/扩仓保护的主路径**：交易所服务端原子生效，全程不存在
    「零张止损」或「两张止损」的中间窗口——严格优于「先挂新单再撤旧单」的替换法
    （后者仅作 amend 失败，例如算法单已触发/已消失时的兜底）。

    三个新值全为 None 时不下发（调用方逻辑错误）→ 直接返回 ok=False，不发空命令。
    `--newSlOrdPx=-1`/`--newTpOrdPx=-1` 必须用等号形式：值以短横开头，空格分隔会被
    commander.js 当成另一个 flag（见 place_market_open 里同款历史坑）。
    """
    if new_sl_trigger_px is None and new_tp_trigger_px is None and new_sz is None:
        return {"ok": False, "sCode": None, "sMsg": "amend_no_change_requested",
                "data": [], "raw": None, "error": "amend_no_change_requested"}
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"algoId": algo_id, "sCode": "0"}], "dryrun": True,
                "amended": {"sl": new_sl_trigger_px, "tp": new_tp_trigger_px,
                            "sz": new_sz}}
    args = ["swap", "algo", "amend", "--instId", inst_id, "--algoId", str(algo_id)]
    if new_sz is not None:
        args += ["--newSz", str(new_sz)]
    if new_sl_trigger_px is not None:
        args += ["--newSlTriggerPx", str(new_sl_trigger_px), "--newSlOrdPx=-1"]
    if new_tp_trigger_px is not None:
        args += ["--newTpTriggerPx", str(new_tp_trigger_px), "--newTpOrdPx=-1"]
    return _call(*args, profile=profile)


def cancel_algo_order(inst_id: str, algo_id: str, profile: str) -> dict[str, Any]:
    """撤单（`okx swap algo cancel --instId <id> --algoId <id>`）。

    只用于两处：① amend 失败后的「挂新→撤旧」兜底；② 开仓未成交时清理悬挂保护单
    （此前只能写 repair_queue 等人工）。**绝不用于让持仓变裸仓**——调用方必须
    先确认另一张全仓止损已回读确认（主人拍板 2026-08-13：止损不可撤只能替换）。
    """
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"algoId": algo_id, "sCode": "0"}], "dryrun": True}
    args = ["swap", "algo", "cancel", "--instId", inst_id, "--algoId", str(algo_id)]
    return _call(*args, profile=profile)


def close_position_cli(inst_id: str, mgn_mode: str, pos_side: str,
                       profile: str) -> dict[str, Any]:
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"sCode": "0"}], "dryrun": True}
    args = ["swap", "close", "--instId", inst_id, "--mgnMode", mgn_mode,
            "--posSide", pos_side]
    return _call(*args, profile=profile)


def place_reduce_only_market(inst_id: str, pos_side: str, sz: float, profile: str,
                             mgn_mode: str = "cross") -> dict[str, Any]:
    """reduceOnly 反向市价单（2026-07-03 起为 close 主路径，可拿 ordId 即时确认；
    swap close 降为兜底。reduceOnly 保证绝不翻反向仓）。"""
    close_side = "sell" if pos_side == "long" else "buy"
    if is_dryrun():
        return {"ok": True, "sCode": "0", "sMsg": "DRYRUN",
                "data": [{"ordId": "DRYRUN-REDUCE", "sCode": "0"}], "dryrun": True}
    args = ["swap", "place", "--instId", inst_id, "--side", close_side,
            "--ordType", "market", "--sz", str(sz), "--posSide", pos_side,
            "--tdMode", mgn_mode, "--reduceOnly"]
    return _call(*args, profile=profile)
