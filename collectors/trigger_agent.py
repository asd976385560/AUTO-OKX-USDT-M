# -*- coding: utf-8 -*-
"""V2.0 起棒适配层：用 `openclaw agent` 拉起 stage 对应 agent 的一个 turn。

现役定位：**唯一 caller = core/dispatcher.py**（_fire_stage 抢 stage_dispatch 闩锁后调
fire()）；本模块集中管理 agent-id / 分级 timeout / --message-file 主路径，自身不做幂等
（幂等真值 = ledger.stage_dispatch）。

Agent stage（analyst/live）与纯脚本 push stage 均由 core/dispatcher.py 确定性起棒。

三条设计红线
------------
1. **零模型名**（红线 #8）：本模块只含 agent-id / session-key（路由标识，非模型）。
   模型分配只在 `openclaw config agents.list.<id>.model`，本桥永不碰。
2. **每 cycle 独立 session**：session-key 带 cycle 槽位 → 每轮每 agent 一个新会话，
   单轮跑完即弃。根治持久会话 context overflow（OKXV7 2026-06-18 13:01 事故）。
   所有跨轮状态在 DB（analysis.db / *_trades.db），不靠会话记忆。
3. **detached 异步启动**：闩锁赢家立即返回，不阻塞等 agent turn 跑完——采集脚本有
   硬超时（快采 ≤360s），analyst turn 可能数分钟。Gateway 服务端跑 turn，CLI 客户端
   detached 退出不影响。

用法
----
作为库（现役主路径，core/dispatcher._fire_stage 调）：
    import trigger_agent
    trigger_agent.fire(stage, cycle_id, mode)   # -> session-key

作为 CLI（人工排障/补起单棒用）：
    python trigger_agent.py --stage live --cycle 2026-06-18T14:00
    python trigger_agent.py --stage push --cycle 2026-06-18T14:00  # 永久走 push_pipeline.py

环境变量（覆盖默认 agent-id / 二进制 / dry-run）：
    OKX_ANALYST_AGENT  OKX_LIVE_AGENT
    OKX_OPENCLAW_BIN（显式自定义 launcher 时保留原路径）
    OKX_OPENCLAW_AGENT_ADAPTER（默认同连接取消适配器）
    OKX_LAUNCH_PROBE_S（默认 3 秒；检测子进程启动后立即非零退出）
    OKX_STAGE_RUNNER / OKX_STAGE_STATUS_DIR（终态监督脚本 / 状态目录）
    OKX_TRIGGER_DRYRUN=1（不真起 agent，只把命令写日志，用于 tmp 验证 plumbing）
"""
from __future__ import annotations


def _public_project_path(*parts):
    """Resolve this public checkout without a host-specific fallback."""
    import os
    from pathlib import Path
    root = Path(os.environ.get('OKX_ROOT') or Path(__file__).resolve().parents[1])
    return str(root.joinpath(*parts))


import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

if _public_project_path() not in sys.path:
    sys.path.insert(0, _public_project_path())
from core.decision_card import compact_text  # noqa: E402
from core.candidate_bundle_runtime import (  # noqa: E402
    prepare_candidate_bundle,
)
from scripts import _acceptance_thresholds as thresholds  # noqa: E402
from scripts.multitimeframe_decision_evidence import (  # noqa: E402
    candidate_evidence_paths,
    load_candidate_manifest,
)
from scripts import _zh_labels as zh_labels  # noqa: E402
from core.risk_validator import (  # noqa: E402
    MAX_PORTFOLIO_IMR_RATIO,
    MAX_SINGLE_ORDER_IMR_RATIO,
    SINGLE_ORDER_SIZING_HEADROOM_PCT,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

CST = timezone(timedelta(hours=8))

# stage -> agent-id。env 可覆盖。
# 主链由 okx-live-trader 合并承担分析+实盘；analyst 映射只服务主人明确要求的人工回滚。
# push 不属于 Agent 路由；fire(stage="push") 无条件起纯脚本管道。
STAGE_AGENTS = {
    "analyst": os.environ.get("OKX_ANALYST_AGENT", "okx-analyst"),
    "live": os.environ.get("OKX_LIVE_AGENT", "okx-live-trader"),
}

# 显式 agent turn 超时（秒）：人工回滚 analyst 900；full live 720；
# push 纯脚本不使用 Agent timeout。
# 勿盲目加大：超时=挂死会话占据 gateway lane 的上限（拥塞治理约束）。
STAGE_TIMEOUTS = {
    "analyst": int(os.environ.get("OKX_ANALYST_TIMEOUT_S", "900")),
    "live": int(os.environ.get("OKX_TRADER_TIMEOUT_S", "720")),
}
# 合并轮的配置上限仍保留 1500s；真正下发给 Gateway 的 timeout 会再按
# ``cycle+12:00`` 的内部绝对时钟收紧。该内部预算只让 Gateway turn 提前让出
# 模型工具时间，不是 §6 停表点；V4 的业务终态与落记录收尾上界继续由
# ``_acceptance_thresholds.py`` 解析，报告/日志/Push 均在 870 秒停表后独立验收。
# 人工回滚后的 full live 沿用上面的 720s。
UNIFIED_LIVE_TIMEOUT = int(os.environ.get("OKX_UNIFIED_LIVE_TIMEOUT_S", "1500"))
# 15 分钟固定节奏下的业务完成目标，不是硬杀进程超时。统一轮应把主要时间留给
# 当轮判断与确定性落库；25 分钟 hard timeout 仍只负责兜住真正挂死，避免在订单或
# writer 临界区强杀。候选深挖数只约束串行取证成本，不限制 Agent 的多空、退出或
# 保护调整裁决权。深挖 3..8 是动态评估区间，不是最低开仓数或
# 方向配额；最终 signals 仍允许为空。
UNIFIED_ANALYSIS_TARGET_MIN = 11
UNIFIED_FINALIZE_RESERVE_MIN = 3
# 整点槽收口预留：slow/regime 采集尾巴 + 全部持仓复核优先，候选只用真实剩余。
UNIFIED_FINALIZE_RESERVE_HOURLY_MIN = 4
UNIFIED_OPEN_REVIEW_MIN = 3
UNIFIED_OPEN_FINALISTS = 8
# 最终开仓短名单容量与深挖上限解耦：signals 仍恒为 0..3。
UNIFIED_OPEN_SIGNAL_CAP = 3
UNIFIED_GATEWAY_DEADLINE_SECONDS = 14 * 60


from collectors.cycle_contract import validate_cycle_id, cycle_session_token, cycle_status_token


def _unified_live_timeout_seconds(
    cycle_id: str,
    *,
    now: datetime | None = None,
) -> int:
    """Return the bounded Gateway run budget for one unified live cycle.

    ``openclaw agent --timeout`` is forwarded to the Gateway as the actual
    agent-run timeout.  Keeping this internal turn budget earlier than the
    threshold-resolved V4 business/finalization guards prevents the
    Gateway-owned turn from continuing to call tools after its local CLI
    process has already been terminated; it does not redefine the SLA stop.
    """
    cycle_start = datetime.strptime(
        str(cycle_id), "%Y-%m-%dT%H:%M",
    ).replace(tzinfo=CST)
    current = now or datetime.now(CST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CST)
    current = current.astimezone(CST)
    deadline = cycle_start + timedelta(
        seconds=UNIFIED_GATEWAY_DEADLINE_SECONDS)
    remaining = int((deadline - current).total_seconds())
    return max(1, min(UNIFIED_LIVE_TIMEOUT, remaining))

# 直起 node + openclaw.mjs（绕开 openclaw.cmd）：
#   ① 不经 cmd.exe → 无控制台弹窗（CREATE_NO_WINDOW 对 .cmd 仍会闪 cmd 窗）；
#   ② 消息主路径使用 --message-file（UTF-8 文件官方契约 + 每轮落盘审计，
#     见 _write_message_file）；中文 --message 仅作为
#     文件写失败时的 argv 兜底路径依据。
_NODE_BIN = os.environ.get("OKX_NODE_BIN", r"C:\Program Files\nodejs\node.exe")
_OPENCLAW_MJS = os.environ.get(
    "OKX_OPENCLAW_MJS",
    '<USER_HOME>\\AppData\\Roaming\\npm\\node_modules\\openclaw\\openclaw.mjs'.replace('<USER_HOME>', str(__import__('pathlib').Path.home())),
)
_OPENCLAW_AGENT_ADAPTER = os.environ.get(
    "OKX_OPENCLAW_AGENT_ADAPTER",
    _public_project_path('scripts', 'openclaw_agent_same_connection.mjs'),
)
# 兼容：设了 OKX_OPENCLAW_BIN 则仍用单一 bin（自定义 wrapper）；否则走 node+mjs。
OPENCLAW_BIN = os.environ.get("OKX_OPENCLAW_BIN", "")

# push stage 固定执行纯脚本 push_pipeline.py，避免 LLM 临场拼装报告产生结构漂移。
# 起法用 **python.exe 直起**（原生 exe，同 node 路径可 DETACHED 存活）——不经 pwsh wrapper：
#   pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行。
#   push_pipeline 自身只读库 + 内部各步仍走 wrapper（拿 UTF-8/PYTHONPATH/MX_APIKEY），故裸 python 起足够。
_PUSH_PIPELINE = os.environ.get("OKX_PUSH_PIPELINE", _public_project_path('scripts', 'push_pipeline.py'))
_PYTHON_EXE = os.environ.get(
    "OKX_PYTHON_BIN",
    sys.executable)
_STAGE_RUNNER = os.environ.get(
    "OKX_STAGE_RUNNER", _public_project_path('scripts', 'stage_runner.py'))
_STAGE_STATUS_DIR = Path(os.environ.get(
    "OKX_STAGE_STATUS_DIR", _public_project_path('logs', 'stage-status')))
_OKX_DB_ROOT = os.environ.get("OKX_DB_ROOT", _public_project_path('db'))


def _root_namespace(db_root: str | os.PathLike | None = None) -> str:
    """Return a stable suffix for artifacts bound to an isolated DB root."""
    resolved = Path(db_root or _OKX_DB_ROOT).resolve()
    if os.path.normcase(os.fspath(resolved)) == os.path.normcase(
        os.fspath(_CANONICAL_DB_ROOT)
    ):
        return ""
    return "r" + hashlib.sha256(
        os.path.normcase(os.fspath(resolved)).encode("utf-8")
    ).hexdigest()[:10]


def _stage_status_path(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> Path:
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return _STAGE_STATUS_DIR / f"{stage}-{cycle_status_token(cycle_id)}{tail}.json"


def _resolve_db_root(db_root: str | os.PathLike | None = None) -> Path:
    return Path(db_root or _DB_ROOT).resolve()


_PROJECT_ROOT = Path(_public_project_path()).resolve()
_CANONICAL_DB_ROOT = (_PROJECT_ROOT / 'db').resolve()

def _launcher() -> list[str]:
    """起棒命令前缀：优先 node + openclaw.mjs；env 覆盖或 mjs 缺失时兜底。"""
    if OPENCLAW_BIN:
        return [OPENCLAW_BIN]
    if Path(_OPENCLAW_MJS).exists() and Path(_NODE_BIN).exists():
        # --stack-size=8192：node 一开始就带大栈，OpenClaw entry.js 的 hasStackSizeConfigured()
        # 检测到即**不 re-spawn worker**——worker re-spawn 没设 windowsHide、Windows 下 detached 被强制
        # false，会自建控制台被 Windows Terminal DefTerm 弹「openclaw-agent」窗。绕过 respawn = 单进程
        # （配合 fire() 的 DETACHED 无控制台）= 不弹窗。见 entry.js:86-90 spawn + :290 hasStackSizeConfigured。
        return [_NODE_BIN, "--stack-size=8192", _OPENCLAW_MJS]
    # 兜底（弹窗/坏码风险）：仅当 node/mjs 缺失
    return ['<USER_HOME>\\AppData\\Roaming\\npm\\openclaw.cmd'.replace('<USER_HOME>', str(__import__('pathlib').Path.home()))]


def _agent_launcher() -> list[str]:
    """Agent launcher with an in-process same-connection cancel bridge.

    ``OKX_OPENCLAW_BIN`` remains an explicit compatibility override.  On the
    normal node+mjs path, the adapter imports the official CLI in the same Node
    process; stage_runner can therefore ask OpenClaw's own SIGTERM handler to
    issue ``chat.abort`` on the originating Gateway connection before the
    bounded process-tree fallback runs.
    """
    if OPENCLAW_BIN:
        return _launcher()
    if Path(_OPENCLAW_MJS).exists() and Path(_NODE_BIN).exists():
        return [
            _NODE_BIN,
            "--stack-size=8192",
            _OPENCLAW_AGENT_ADAPTER,
            "--openclaw-mjs",
            _OPENCLAW_MJS,
            "--",
        ]
    return _launcher()


LOG_DIR = Path(_public_project_path('logs', 'trigger'))

# Windows detached 启动标志：子进程脱离父，父退出不带走它。
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _launch_probe_seconds() -> float:
    try:
        return min(max(float(os.environ.get("OKX_LAUNCH_PROBE_S", "3")), 0.0), 10.0)
    except (TypeError, ValueError):
        return 3.0


def _probe_launch(
    proc: subprocess.Popen,
    stage: str,
    cycle_id: str,
    fh,
    db_root: str | os.PathLike | None = None,
) -> None:
    """短暂观察 detached 子进程；启动期非零退出必须冒泡给 dispatcher 释放闩锁。

    超过探针窗口仍在运行即视为已正常进入主流程，后续完成仍由 DB 生命周期记录判定，
    不把异步 turn 改成同步等待。
    """
    probe_s = _launch_probe_seconds()
    try:
        rc = proc.wait(timeout=probe_s)
    except subprocess.TimeoutExpired:
        return
    fh.write(f"  launch_probe: process exited during {probe_s:g}s probe rc={rc}\n")
    fh.flush()
    if rc != 0:
        # supervised runner 已成功启动且明确记录 child=failed 时，属于业务终态失败：
        # 闩锁必须保留（只告警不重试），不能让 dispatcher 当“起棒失败”释放后重派。
        status_path = _stage_status_path(stage, cycle_id, db_root)
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state = {}
        if (state.get("status") == "failed"
                and int(state.get("runner_pid") or -1) == int(proc.pid)):
            fh.write("  launch_probe: supervised child failed; stage latch retained "
                     "(alert-only, no retry)\n")
            fh.flush()
            return
        raise RuntimeError(
            f"{stage} cycle={cycle_id} child exited during launch probe rc={rc}"
        )


def _supervised_cmd(
    stage: str,
    cycle_id: str,
    mode: str,
    command: list[str],
    db_root: str | os.PathLike | None = None,
) -> list[str]:
    """独立 runner 等待 detached 真子进程并持久化终态；不负责释放闩锁或重试。"""
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = os.fspath(Path(db_root or _OKX_DB_ROOT).resolve())
    return [
        _PYTHON_EXE, _STAGE_RUNNER,
        "--stage", stage, "--cycle", cycle_id, "--mode", mode,
        "--db-root", resolved_db_root,
        "--", *command,
    ]


def now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def _safe_cycle(cycle_id: str) -> str:
    """'2026-06-18T14:00' -> '20260618-1400'（session-key 不含冒号，避免分段歧义）。"""
    return cycle_session_token(cycle_id)


def session_key(
    stage: str,
    cycle_id: str,
    db_root: str | os.PathLike | None = None,
) -> str:
    """bare key（不带 agent: 前缀）；交给 openclaw --agent 拼成 agent:<id>:<key>。

    （MEMORY 教训：自己拼前缀会变 4 段双前缀 → setup timeout。）
    """
    suffix = _root_namespace(db_root)
    tail = f"-{suffix}" if suffix else ""
    return f"{stage}-{_safe_cycle(cycle_id)}{tail}"


# 操作手册＝各 agent 的 workspace AGENTS.md（OpenClaw 每轮自动加载）；fire 消息只指 AGENTS.md。


_DB_ROOT = Path(os.environ.get("OKX_DB_ROOT", _public_project_path('db')))
_AUTOHEAL_CONTRACT_VERSION = 1


def _autoheal_client_result(profile: str | None, cycle_id: str,
                            request_id: str, *, status: str, rc: int,
                            reason: str, db_root: Path | None = None,
                            finding_kind: str | None =
                            "AUTOHEAL-CONTRACT-INVALID") -> dict:
    """Build a caller-side result when no trustworthy producer result exists."""
    root = Path(db_root or _DB_ROOT).resolve()
    finding = ({"kind": finding_kind, "sev": "P1", "reason": reason}
               if finding_kind else None)
    return {
        "contract_version": _AUTOHEAL_CONTRACT_VERSION,
        "request_id": request_id,
        "profile": profile,
        "cycle": cycle_id,
        "db_root": str(root),
        "status": status,
        "applied": False,
        "p0": False,
        "blocking": rc != 0,
        "reason": reason,
        "findings": [finding] if finding else [],
        "healed": [],
        "needs_human": [finding] if finding else [],
        "rc": rc,
    }


def _read_autoheal_contract(path: Path, *, request_id: str, profile: str,
                            cycle_id: str, db_root: Path,
                            returncode: int) -> dict:
    """Read and strictly bind a v1 result to this exact subprocess request."""
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 2_000_000:
            raise ValueError("json-out exceeds 2 MB")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("json-out root is not an object")
        checks = {
            "contract_version": data.get("contract_version") == _AUTOHEAL_CONTRACT_VERSION,
            "request_id": data.get("request_id") == request_id,
            "profile": data.get("profile") == profile,
            "cycle": data.get("cycle") == cycle_id,
            "db_root": Path(str(data.get("db_root") or "")).resolve()
                       == db_root.resolve(),
            "status": data.get("status") in {
                "ok", "applied", "needs_human", "error", "skipped",
                "p0_blocked",
            },
            "applied": type(data.get("applied")) is bool,
            "p0": type(data.get("p0")) is bool,
            "blocking": type(data.get("blocking")) is bool,
            "findings": isinstance(data.get("findings"), list),
            "healed": isinstance(data.get("healed"), list),
            "needs_human": isinstance(data.get("needs_human"), list),
            "rc": type(data.get("rc")) is int
                  and data.get("rc") in (0, 1, 2, 3, 4),
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise ValueError("contract fields invalid: " + ",".join(failed))
        if data["blocking"] != (data["rc"] != 0):
            raise ValueError("blocking/rc mismatch")
        if data["p0"] != (data["rc"] == 4):
            raise ValueError("p0/rc mismatch")
        status_by_rc = {
            0: {"ok", "applied"}, 1: {"needs_human"}, 2: {"error"},
            3: {"skipped"}, 4: {"p0_blocked"},
        }
        if data["status"] not in status_by_rc[data["rc"]]:
            raise ValueError("status/rc mismatch")
        healed_applied = any(
            isinstance(item, dict) and item.get("applied") is True
            for item in data["healed"])
        if data["applied"] != healed_applied:
            raise ValueError("applied/healed mismatch")
        findings_p0 = any(
            isinstance(item, dict)
            and str(item.get("sev") or "").upper() == "P0"
            for item in data["findings"])
        if data["p0"] != findings_p0:
            raise ValueError("p0/findings mismatch")
        if int(returncode) != data["rc"]:
            raise ValueError(
                f"process rc={returncode} differs from contract rc={data['rc']}")
        return data
    except Exception as exc:  # noqa: BLE001
        return _autoheal_client_result(
            profile, cycle_id, request_id, status="contract_invalid", rc=2,
            reason=f"{type(exc).__name__}: {exc}", db_root=db_root,
        )


def _run_briefing(
    cycle_id: str | None = None,
    *,
    candidate_out_file: Path | None = None,
    ready_pool_out_file: Path | None = None,
) -> str:
    """跑一次 decision_briefing（五库简报）。失败返回空串（agent 退回自查，不阻断）。

    2026-08-18：派发预读带 --cycle-id → 简报同时把两层候选追加写
    logs/briefing/candidates-*.jsonl（错失池 briefing_layer_v1 源 +
    连续候选轮数的确定性来源）；不带参数的手动补跑不写快照。"""
    try:
        brief_py = Path(__file__).parent.parent / "scripts" / "decision_briefing.py"
        cmd = [sys.executable, str(brief_py), "--db-root", str(_DB_ROOT)]
        if cycle_id:
            cmd += ["--cycle-id", str(cycle_id)]
        if candidate_out_file is not None:
            if not cycle_id:
                return ""
            cmd += ["--candidate-out-file", str(candidate_out_file)]
        if ready_pool_out_file is not None:
            if not cycle_id:
                return ""
            cmd += ["--ready-pool-out-file", str(ready_pool_out_file)]
        p = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=_CREATE_NO_WINDOW,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


_CLOSURE_BRIEF_RETIRED_MARKERS = (
    "三周期", "六项", "15m/1h/4h", "mtf", "multitimeframe",
    "find_similar", "evidence_contract", "decision_card",
    "candidate_id", "candidate identity", "exact candidate",
    "exact manifest", "四态",
    "entry_ready", "extended", "triggering", "early_watch",
)
_CLOSURE_BRIEF_RETIRED_TIMEFRAME_RE = re.compile(
    r"(?:"
    r"(?<![a-z0-9])(?:15m|1h|4h)(?![a-z0-9])|"
    r"(?:15分钟|十五分钟|1小时|一小时|4小时|四小时)"
    r"(?:级别|周期|k线|结构|方向|信号|趋势|确认|证据)|"
    r"(?:高低|高/低|高、低|多|跨|长短|大小|高|低)(?:级别)?周期|"
    r"(?:周期|级别)(?:共振|一致|对齐|确认|授权|证据|方向|冲突)|"
    r"(?:multi(?:ple)?|cross|higher|lower)[-_\s]*"
    r"(?:timeframe|time[-_\s]*frame|tf)s?|"
    r"(?:timeframe|time[-_\s]*frame)[-_\s]*"
    r"(?:align(?:ment)?|confirm(?:ation)?|conflict|confluence)|"
    r"(?<![a-z0-9])(?:timeframe|time[-_\s]*frame)s?"
    r"(?![a-z0-9])|"
    r"(?<![a-z0-9])(?:htf|ltf)(?![a-z0-9])"
    r")",
    re.IGNORECASE,
)
_CLOSURE_BRIEF_SOFT_VETO_RE = re.compile(
    r"(?:"
    r"(?:成交额|成交量|oi)(?:不足|偏低|过低|太低|低于|低|门槛)|"
    r"成交(?:不活跃|清淡|稀少)|"
    r"(?:流动性|量能|成交活跃度)(?:不足|过低|差|弱|不活跃|门槛)|"
    r"(?:无|缺|缺乏|没有)(?:催化|新闻|事件驱动|消息驱动)|"
    r"(?:已有|现有|当前)(?:\d+|[零一二三四五六七八九十百两]+)?"
    r"(?:个)?(?:仓|仓位)|"
    r"(?:仓位|持仓)(?:过多|太多|较多|已满|满载)|"
    r"(?:imr|保证金|风控预算|风险预算).{0,8}"
    r"(?:紧张|不足|接近|偏高|过高)"
    r")",
    re.IGNORECASE,
)
_CLOSURE_BRIEF_NON_GATE_RE = re.compile(
    r"(?:"
    r"仅参与(?:连续)?排序|只参与.{0,8}排序|仅作观察|只作观察|"
    r"不是门槛|非门槛|不作为.{0,8}门槛|"
    r"(?:不得|不能|不可).{0,8}单独.{0,8}(?:reject|拒绝|否决)"
    r")",
    re.IGNORECASE,
)


def _closure_brief_projection(brief: str) -> str:
    """Drop retired operational clauses from the closure-era prompt."""
    kept: list[str] = []
    for line in str(brief or "").splitlines():
        folded = line.casefold()
        retired_contract = any(
            marker in folded for marker in _CLOSURE_BRIEF_RETIRED_MARKERS)
        retired_timeframe = bool(
            _CLOSURE_BRIEF_RETIRED_TIMEFRAME_RE.search(line))
        soft_veto = bool(_CLOSURE_BRIEF_SOFT_VETO_RE.search(line))
        explicit_non_gate = bool(_CLOSURE_BRIEF_NON_GATE_RE.search(line))
        if retired_contract or retired_timeframe:
            continue
        if soft_veto and not explicit_non_gate:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _closure_unified_live_message(
    cycle_id: str,
    brief: str,
    *,
    candidate_bundle: dict | None,
) -> str:
    """Return the concise, forward-only full-closure natural-run contract."""
    safe_cycle = cycle_id.replace(":", "-")
    bundle = dict(candidate_bundle or {})
    paths = candidate_evidence_paths(cycle_id)
    manifest_path = str(bundle.get("manifest_path") or paths["manifest"])
    review_path = str(
        bundle.get("decision_slice_path") or paths["decision_slice"])
    manifest_count = bundle.get(
        "manifest_count", bundle.get("candidate_count", "unknown"))
    review_count = bundle.get(
        "decision_slice_count", bundle.get("candidate_count", "unknown"))
    facts_path = (f"<PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    view_path = (f"<PROJECT_ROOT>/tmp/position_exit_view_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    handoff_path = (f"<PROJECT_ROOT>/tmp/live_input_handoff_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    analysis_path = (f"<PROJECT_ROOT>/tmp/analysis_receipt_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    plan_path = (f"<PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    receipt_path = (f"<PROJECT_ROOT>/tmp/_receipt_live_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    terminal = (
        datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
        + timedelta(
            seconds=thresholds.sla_business_terminal_deadline_seconds(
                cycle_id))
    ).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    projected_brief = _closure_brief_projection(brief)
    analysis_ts = cycle_id.replace("T", " ") + ":00"
    coverage_limit = (
        int(review_count)
        if isinstance(review_count, int) and not isinstance(review_count, bool)
        else UNIFIED_OPEN_FINALISTS
    )
    analysis_skeleton = json.dumps({
        "cycle_id": cycle_id,
        "ts": analysis_ts,
        "mode": "full",
        "status": "ok",
        "decision_protocol": "minimal_decision_v2",
        "regime": "range",
        "regime_stale": 0,
        "market_summary": {
            "macro": {},
            "news": {"events": []},
            "tech": {},
            "sentiment": {},
            "quant": {},
        },
        "missing_sources": [],
        "signals": [{
            "symbol": "REPLACE_WITH_REVIEW_SYMBOL-USDT-SWAP",
            "action": "open_long",
            "side": "long",
            "reasoning": "replace with current-cycle independent evidence",
            "entry_hint": None,
            "stop_hint": None,
            "tp_hint": None,
            "exit_mode": "fixed_tp",
        }],
        "raw": {
            "candidates_deep_dived_v2": [{
                "symbol": "REPLACE_WITH_REVIEW_SYMBOL-USDT-SWAP",
                "decision": "provisional_open",
                "reason": "replace with the same current-cycle evidence",
            }],
            "candidate_coverage": {
                "dynamic_limit": coverage_limit,
                "stop_reason": "target_reached",
            },
        },
    }, ensure_ascii=False, indent=2)
    brief_block = (
        "\n【本轮只读市场简报】\n"
        f"--- decision_briefing ---\n{projected_brief}\n--- end ---\n"
        if projected_brief else
        "\n【本轮市场简报无可消费内容：按失败关闭完成analysis终态】\n"
    )
    facts_command = (
        ("pwsh -NoProfile -NonInteractive -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 "
        "<PROJECT_ROOT>/scripts/live_decision_facts.py "
        f"--profile live --cycle-id {cycle_id} --out-file {facts_path} "
        "--analysis-db <PROJECT_ROOT>/db/analysis.db").replace('<PROJECT_ROOT>', _public_project_path())
    )
    runner_command = (
        ("pwsh -NoProfile -NonInteractive -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 "
        "<PROJECT_ROOT>/scripts/live_position_action_runner.py "
        f"--cycle-id {cycle_id} --plan-file {plan_path} "
        f"--facts-file {facts_path} --receipt-file {receipt_path} "
        "--db-root <PROJECT_ROOT>/db").replace('<PROJECT_ROOT>', _public_project_path())
    )
    return (
        (f"OKX 本轮工作：stage=live cycle={cycle_id} dispatch_mode=unified；"
        "policy=minimal_contract_full_closure_v1。cycle只认本消息，不按墙钟重算。"
        "【顺序】先执行采集gate；gate通过后完成analysis并以完整UTF-8 JSON先"
        "validate-only、再用完全相同文件正式调用analyst_writer。正式writer返回ok:true前，"
        "不得读取私有账户、不得生成plan。analysis固定mode=full,status=ok、"
        "decision_protocol=minimal_decision_v2，market_summary恰含macro/news/tech/"
        "sentiment/quant五个object；signals为JSON list；raw保存review与coverage。"
        "【analysis完整JSON骨架】下面是一份语法完整的顶层object；必须替换示例symbol/"
        "regime/事实，禁止删除任何顶层键。ts必填，格式固定YYYY-MM-DD HH:MM:SS，"
        f"本轮可直接使用{analysis_ts}。一次write到{analysis_path}：\n"
        f"```json\n{analysis_skeleton}\n```\n"
        "【价格单位】骨架的三个null必须替换为该symbol同轮last对应的USDT绝对价格；"
        "null仅表示待填写，不能通过OPEN校验。先读取具名review中该symbol的last，"
        "简报同行也展示last；不得照抄示例、将价格归一成1或把百分比当价格。"
        "entry_hint/stop_hint/tp_hint由你基于该实际价位判断，runner不会把比例换算成价格。"
        "缺少可核验现价时不得猜价，按价格证据缺失记录该symbol的reject。"
        f"【全市场review】manifest={manifest_path}，manifest_count={manifest_count}；"
        f"只读具名review={review_path}，review_count={review_count}。每个review symbol只写"
        "provisional_open或reject；OPEN方向在signal中选择long|short。成交额、OI、无催化、"
        "已有仓位数或尚未触发的组合上限不得单独构成reject；真实点差、滑点、深度、价格失效"
        "以及确定性真钱硬闸仍可形成可复核理由。不设最低开仓数。"
        "【soft-only确定性处理】成交额/量能/OI偏低、流动性泛称、无催化、微观N/A、"
        "已有仓位数或尚未触发的IMR/预算，单独或组合都不足以写reject。只有具名实时点差、"
        "滑点、订单簿深度、价格失效/方向冲突等独立执行证据，或明确已触发的真钱硬闸，"
        "才允许reject。若没有这类独立证据/硬闸，必须写decision=provisional_open并给同symbol"
        "唯一open_long|open_short signal，让后续facts/risk_validator/executor硬闸裁决；"
        "禁止用低量、低OI或微观N/A换词偷渡reject。"
        "【analysis writer固定调用】完整JSON只构造一次后，下一条工具调用必须是："
        "pwsh -NoProfile -NonInteractive -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 "
        f"<PROJECT_ROOT>/collectors/analyst_writer.py --input-file {analysis_path} --validate-only。"
        "若返回ok:true，下一条工具调用必须去掉--validate-only并以完全相同文件正式writer，"
        "中间禁止read/edit/query/回复文字。若validate-only返回ok:false，只按返回的精确error"
        "整文件修正一次，再validate-only一次；第二次仍失败即停止。修正期间禁止其它工具、"
        "源码/schema/旧回执查询或局部补丁。"
        "【确定性私有事实交接】analysis正式落库后，stage_runner监督进程自动且只执行一次"
        f"固定只读命令：{facts_command}。Agent禁止自行运行该命令、禁止直查私有API、"
        "禁止自行换算账户或仓位字段。监督进程随后生成逐仓精简view、自校验cycle/profile/"
        "status/hash/position_count，并原子写交接状态。你只允许读取具名"
        f"handoff={handoff_path}；status=waiting_for_analysis|preparing时，仅重复读取这个具名handoff；"
        f"status=ready后依次读取facts={facts_path}和view={view_path}。"
        "handoff=failed或hash/cycle/count不一致时立即失败关闭，禁止plan；facts.status=blocking"
        "时禁止OPEN/ADD，只能采用action_policy.allowed_executor_actions明确列出的去风险动作，"
        "否则HOLD并保留阻断原因；"
        "stage监督与runner还会独立重验，因此不能跳过该输入。"
        "【plan发布】先write具名草稿，不直接写canonical plan。使用严格JSON序列化；"
        "字符串内换行必须转义，禁止NaN/Infinity或重复键。receipt_context只含cycle_id/mode/status/regime/"
        "decision_protocol/reasoning/position_reviews；每个现仓明确HOLD/CLOSE/REDUCE/"
        "ADJUST_PROTECTION及理由。OPEN/ADD action只写action/symbol/side/"
        "target_stop_risk_pct_equity/lev；CLOSE/REDUCE/ADJUST_PROTECTION写固定动作参数。"
        "analysis中每一个OPEN signal必须在首版plan一一对应为OPEN或ADD：同symbol+side"
        "已有仓时用ADD，否则用OPEN；不得因当前IMR尚未触顶、仓位数或预计硬闸而提前"
        "省略，交由runner/executor逐笔裁决。若同仓还需修保护，只允许"
        "ADJUST_PROTECTION与ADD各一项并存。"
        f"草稿=<PROJECT_ROOT>/tmp/position_plan_draft_{safe_cycle}.json；canonical plan={plan_path}。"
        "草稿write返回后下一条工具调用必须执行：pwsh -NoProfile -NonInteractive -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/write_position_plan.py "
        f"--cycle-id {cycle_id}。publisher返回ok:true后，下一条工具调用必须原样执行：{runner_command}。"
        "若publisher返回ok:false且may_rewrite:true，只按精确error整文件修正同一草稿一次，"
        "立即重调同一publisher；第二次失败或may_rewrite:false立即停止。"
        "只有尚无任何交易副作用的failed_preflight才沿用现有唯一整文件修正额度；"
        "执行已开始/已终止或handoff已撤销时禁止改plan。禁止手工写publication/validation标记。"
        "禁止临时脚本、手写SQL、直接订单、改参循环逼近或以HOLD覆盖失败。"
        "【真钱硬闸】账户求真、账仓一致、intent幂等、方向正确SL、杠杆<=10x、"
        "单笔增量保证金<=15%、单笔止损风险<=5%、可用USDT<=98%、名义>=净值1%、"
        "预计组合IMR<=66.6%、成交确认及保护回读全部保持；任一不可证明即失败关闭。"
        f"业务终态必须严格早于{terminal}。runner成功后立即简短终答，不等待Push、"
        "不读历史回执、不补查；runner partial/failed/uncertain或非竞态非零退出即保留真实"
        "副作用并停止新增动作。"
        f"{brief_block}").replace('<PROJECT_ROOT>', _public_project_path())
    )


def _unified_live_message(
    cycle_id: str,
    brief: str,
    *,
    now: datetime | None = None,
    candidate_bundle: dict | None = None,
) -> str:
    """Build the unified-route prompt with the writer contract in-band.

    ``unified`` is a dispatcher routing mode, while analysis_runs.mode has
    always been ``full``.  Keeping that distinction only in a long workspace
    manual proved too easy to lose next to a large briefing, so the money-path
    handoff states the minimal receipt shape explicitly.
    """
    safe_cycle = cycle_id.replace(":", "-")
    if thresholds.minimal_contract_closure_active(cycle_id):
        return _closure_unified_live_message(
            cycle_id,
            brief,
            candidate_bundle=candidate_bundle,
        )
    relaxed_policy = thresholds.decision_restriction_removal_active(cycle_id)
    minimal_policy = thresholds.minimal_decision_contract_active(cycle_id)
    cycle_start = datetime.strptime(cycle_id, "%Y-%m-%dT%H:%M").replace(tzinfo=CST)
    analysis_deadline_seconds = thresholds.sla_analysis_deadline_seconds(
        cycle_id)
    terminal_deadline_seconds = (
        thresholds.sla_business_terminal_deadline_seconds(cycle_id))
    record_deadline_seconds = (
        thresholds.sla_record_reconcile_deadline_seconds(cycle_id))
    analysis_deadline = cycle_start + timedelta(
        seconds=analysis_deadline_seconds)
    terminal_deadline = cycle_start + timedelta(
        seconds=terminal_deadline_seconds)
    record_deadline = cycle_start + timedelta(seconds=record_deadline_seconds)
    sla_deadline = cycle_start + timedelta(
        seconds=thresholds.COMPLETE_CYCLE_SLA_SECONDS)
    record_reserve_seconds = max(
        0, record_deadline_seconds - terminal_deadline_seconds)
    analysis_deadline_text = analysis_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    terminal_deadline_text = terminal_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    record_deadline_text = record_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    sla_deadline_text = sla_deadline.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    if thresholds.complete_cycle_uses_business_terminal_stop(cycle_id):
        cycle_sla_contract = (
            "本轮 870 秒只验两道事实闸：必需采集源已完成，以及分析+判断+交易已形成"
            f"有效业务终态；业务终态必须严格早于 {sla_deadline_text}。"
            f"回执/写库收尾上界为 {record_deadline_text}，但写库、报告、日志、Push "
            "均不计入 870 秒；Push 另按送达时效审计。"
        )
        time_gate_text = (
            f"本轮业务时间闸：分析、逐仓判断与交易共享同一终态上界 "
            f"{terminal_deadline_text}；不再另设分析验收停表点。{cycle_sla_contract}"
        )
    elif thresholds.complete_cycle_uses_record_reconcile_stop(cycle_id):
        cycle_sla_contract = (
            f"为落记录+账实对账预留 {record_reserve_seconds} 秒；"
            f"账实屏障最迟 {record_deadline_text} 完成，完整周期必须严格早于 "
            f"{sla_deadline_text}。Push 不计入完整周期，另按送达时效审计。"
        )
        time_gate_text = (
            f"本轮绝对时间闸：analysis 最迟 {analysis_deadline_text} 冻结，Agent "
            f"业务终态最迟 {terminal_deadline_text} 落库，{cycle_sla_contract}"
        )
    else:
        cycle_sla_contract = (
            f"为 Push+事后对账预留 {record_reserve_seconds} 秒；"
            f"Push+事后对账最迟 {record_deadline_text} 完成，完整周期必须严格早于 "
            f"{sla_deadline_text}。本 cycle 保持边界前历史停表口径。"
        )
        time_gate_text = (
            f"本轮绝对时间闸：analysis 最迟 {analysis_deadline_text} 冻结，Agent "
            f"业务终态最迟 {terminal_deadline_text} 落库，{cycle_sla_contract}"
        )
    observed_at = now or datetime.now(CST)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=CST)
    observed_at = observed_at.astimezone(CST)
    analysis_remaining_seconds = max(
        0, int((analysis_deadline - observed_at).total_seconds()))
    analysis_budget_label = (
        "分析+判断+交易终态闸"
        if thresholds.complete_cycle_uses_business_terminal_stop(cycle_id)
        else "analysis 绝对闸"
    )
    finalize_reserve_min = (
        UNIFIED_FINALIZE_RESERVE_HOURLY_MIN
        if str(cycle_id).endswith(":00")
        else UNIFIED_FINALIZE_RESERVE_MIN)
    analysis_target_min = (
        UNIFIED_ANALYSIS_TARGET_MIN + UNIFIED_FINALIZE_RESERVE_MIN
        - finalize_reserve_min)
    candidate_tool_budget_seconds = analysis_remaining_seconds
    gateway_turn_budget_seconds: int | None = None
    if thresholds.complete_cycle_uses_business_terminal_stop(cycle_id):
        # V4 removed the separate acceptance-time analysis gate, but the
        # Gateway turn still has an earlier cycle+14 internal timeout and the
        # Agent must retain three minutes for facts/position actions/runner.
        # Budget candidate evidence from the *smaller* live limit after that
        # reserve; otherwise a late hourly start can advertise 5 candidates
        # from the 870s business clock even though the model actually has only
        # ~4.5 minutes before its own timeout.
        gateway_turn_budget_seconds = _unified_live_timeout_seconds(
            cycle_id, now=observed_at)
        candidate_tool_budget_seconds = max(
            0,
            min(analysis_remaining_seconds, gateway_turn_budget_seconds)
            - finalize_reserve_min * 60,
        )
    if candidate_tool_budget_seconds <= 150:
        dynamic_candidate_limit = 1
    elif candidate_tool_budget_seconds <= 300:
        dynamic_candidate_limit = 2
    elif candidate_tool_budget_seconds <= 400:
        dynamic_candidate_limit = 3
    elif candidate_tool_budget_seconds <= 460:
        dynamic_candidate_limit = 4
    elif candidate_tool_budget_seconds <= 510:
        dynamic_candidate_limit = 5
    elif candidate_tool_budget_seconds <= 560:
        dynamic_candidate_limit = 6
    elif candidate_tool_budget_seconds <= 610:
        dynamic_candidate_limit = 7
    else:
        dynamic_candidate_limit = UNIFIED_OPEN_FINALISTS
    observed_at_text = observed_at.strftime("%Y-%m-%d %H:%M:%S UTC+8")
    candidate_budget_note = (
        f"；本轮 Gateway turn 实际上限尚余 {gateway_turn_budget_seconds} 秒，"
        f"扣除 {finalize_reserve_min * 60} 秒 facts/逐仓判断/runner 收尾预留后，"
        f"候选工具预算为 {candidate_tool_budget_seconds} 秒"
        if gateway_turn_budget_seconds is not None else ""
    )
    bundle_info = dict(candidate_bundle or {})
    candidate_funnel_active = thresholds.candidate_funnel_repair_active(
        cycle_id)
    bundle_phase = str(
        bundle_info.get("phase") or thresholds.candidate_bundle_phase(cycle_id))
    bundle_status = str(bundle_info.get("status") or "OFF")
    bundle_healthy = (
        bundle_phase in {"shadow", "consume"}
        and bundle_status == "PASSED"
    )
    raw_ready_count = bundle_info.get("ready_count")
    bundle_ready_count = (
        int(raw_ready_count)
        if isinstance(raw_ready_count, int) and not isinstance(raw_ready_count, bool)
        and raw_ready_count >= 0
        else None
    )
    dynamic_candidate_target = (
        min(dynamic_candidate_limit, bundle_ready_count)
        if bundle_healthy and bundle_ready_count is not None
        else dynamic_candidate_limit
    )
    dynamic_candidate_floor = min(
        UNIFIED_OPEN_REVIEW_MIN, dynamic_candidate_target)
    bundle_path = str(bundle_info.get("bundle_path") or "")
    decision_slice_path = str(
        bundle_info.get("decision_slice_path") or bundle_path)
    manifest_count = bundle_info.get(
        "manifest_count", bundle_info.get("candidate_count"))
    candidate_manifest_path = str(
        bundle_info.get("manifest_path")
        or candidate_evidence_paths(cycle_id)["manifest"])
    ready_pool_path = str(
        bundle_info.get("ready_pool_path")
        or candidate_evidence_paths(cycle_id)["ready_pool"])
    ready_pool_hash = str(bundle_info.get("ready_pool_sha256") or "")
    ready_pool_status = str(
        bundle_info.get("ready_pool_status") or "UNAVAILABLE")
    full_ready_count = bundle_info.get("full_ready_count")
    bundle_candidate_count = bundle_info.get("candidate_count")
    bundle_candidate_count_text = (
        f"{bundle_candidate_count}个"
        if isinstance(bundle_candidate_count, int) else "本轮全部")
    bundle_hash = str(bundle_info.get("bundle_sha256") or "")
    briefing_hash = str(bundle_info.get("briefing_sha256") or "")
    manifest_valid = bundle_info.get("manifest_valid") is True
    if bundle_phase == "shadow" and bundle_healthy:
        bundle_contract_text = (
            "【候选bundle阶段=shadow】本轮已对exact briefing池完成批量只读筛查："
            f"screened={bundle_info.get('screened_count')}/"
            f"{bundle_info.get('candidate_count')}、ready={bundle_ready_count}、"
            f"全ready池={full_ready_count}@{ready_pool_path}、"
            f"ready_pool_status={ready_pool_status}、ready_pool_sha256={ready_pool_hash}、"
            f"briefing_sha256={briefing_hash}、bundle_sha256={bundle_hash}、"
            f"具名bundle={bundle_path}。本阶段严禁用bundle替代逐币决策取证；"
            "逐币取证必须用candidate_id绑定exact manifest，bundle只用于合同等价shadow审计。"
        )
    elif bundle_phase == "consume" and bundle_healthy:
        bundle_contract_text = (
            "【候选bundle阶段=consume·全市场轻量策略】"
            f"全量manifest={manifest_count}、screened="
            f"{bundle_info.get('screened_count')}、决策slice="
            f"{bundle_info.get('decision_slice_count') or bundle_info.get('candidate_count')}、"
            f"slice-ready={bundle_ready_count}、全ready池={full_ready_count}@{ready_pool_path}、"
            f"bundle_sha256={bundle_hash}、decision_slice={decision_slice_path}。"
            "成交额/OI不再准入，manifest无8+8/16项配额；本轮只读有界slice完成"
            "Agent深挖，slice只是既有耗时预算，不是全市场准入上限。"
            if relaxed_policy else
            "【候选bundle阶段=consume】本轮exact briefing池已一次性批量只读筛查："
            f"screened={bundle_info.get('screened_count')}/"
            f"{bundle_info.get('candidate_count')}、ready={bundle_ready_count}、"
            f"全ready池={full_ready_count}@{ready_pool_path}、"
            f"ready_pool_status={ready_pool_status}、ready_pool_sha256={ready_pool_hash}、"
            f"briefing_sha256={briefing_hash}、bundle_sha256={bundle_hash}。"
            f"只允许读取具名bundle={bundle_path}一次并复用逐项evidence_contract；"
            "禁止对bundle内健康项重复运行--symbol。仅当项缺失/哈希漂移时才按"
            "bundle_degraded回退本轮旧逐币路径。"
        )
    elif bundle_phase in {"shadow", "consume"}:
        bundle_contract_text = (
            f"【候选bundle阶段={bundle_phase}，状态={bundle_status}】"
            f"批量证据不可消费，原因={bundle_info.get('error') or 'unavailable'}；"
            + (
                "全市场轻量策略不恢复旧candidate identity、MTF或history门槛；"
                "使用本消息已预读briefing中的有界review候选继续结构化判断；"
                if relaxed_policy else
                "exact manifest仍有效，逐币取证继续使用candidate_id绑定；"
                if manifest_valid else
                "exact manifest无效，04:00后的OPEN身份闸将失败关闭；"
            )
            + "本轮按剩余时间预算收口，不得把bundle降级"
            "记成业务失败或阻断现仓退出。"
        )
    elif candidate_funnel_active and manifest_valid:
        bundle_contract_text = (
            "【候选bundle阶段未消费；候选漏斗独立生效】"
            f"exact manifest={candidate_manifest_path}、"
            f"全ready池={full_ready_count}@{ready_pool_path}、"
            f"ready_pool_status={bundle_info.get('ready_pool_status')}、"
            "逐币取证继续使用candidate_id；不扩大深挖或OPEN上限。"
        )
    elif candidate_funnel_active:
        bundle_contract_text = (
            "【候选漏斗工件异常】exact manifest不可验证；"
            "身份闸激活后OPEN失败关闭，但CLOSE/REDUCE/ADJUST_PROTECTION不受影响。"
        )
    else:
        bundle_contract_text = ""
    candidate_tool_instruction = (
        (
            f"本轮全市场候选只读取有界决策slice={decision_slice_path}；"
            "不得读取MEMORY/DREAMS、不得调用memory_search，也不得为OPEN逐币补跑MTF或"
            "find_similar。slice外候选仍保留在全量manifest，不等于被准入淘汰。"
        )
        if relaxed_policy and bundle_healthy else
        (
            "本轮v2 bundle降级；只使用触发消息内已预读briefing的有界review候选，"
            "不得读取不存在的decision slice，也不得恢复candidate identity、MTF、history或"
            "find_similar门槛。"
        )
        if relaxed_policy else
        f"候选批量证据文件固定为 {bundle_path}；只读该具名文件，禁止列举tmp或logs。"
        if bundle_phase == "consume" and bundle_healthy
        else (
            ("多周期证据命令固定使用exact candidate_id：pwsh -NoProfile -File "
            "<PROJECT_ROOT>/scripts/run_okx_python.ps1 "
            "<PROJECT_ROOT>/scripts/multitimeframe_decision_evidence.py "
            "--db-root <PROJECT_ROOT>/db --candidate-id <cand_...> "
            f"--candidate-manifest-file {candidate_manifest_path} "
            f"--cycle-id {cycle_id} "
            f"--out-file <PROJECT_ROOT>/tmp/mtf_{safe_cycle}_<candidate_id>.json；"
            "symbol/side/layer只认manifest解析值，禁止按简称、别名或自由文本改写。").replace('<PROJECT_ROOT>', _public_project_path())
            if manifest_valid else
            ("多周期证据命令格式：pwsh -NoProfile -File "
            "<PROJECT_ROOT>/scripts/run_okx_python.ps1 "
            "<PROJECT_ROOT>/scripts/multitimeframe_decision_evidence.py "
            "--db-root <PROJECT_ROOT>/db --symbol <完整instId> "
            f"--cycle-id {cycle_id} "
            f"--out-file <PROJECT_ROOT>/tmp/mtf_{safe_cycle}_<symbol>.json；"
            "只读取该文件，禁止手算/hash。").replace('<PROJECT_ROOT>', _public_project_path())
        )
    )
    open_evidence_source_text = (
        "每个open_long/open_short必须绑定本轮bundle中同symbol/side且ready=true的"
        "完整evidence_contract/evidence_hash；只读取上述具名bundle，不得手算/hash，"
        "也不得把NOT_READY项写成OPEN。"
        if bundle_phase == "consume" and bundle_healthy and not relaxed_policy
        else (
            "全市场轻量策略的OPEN只使用本轮有界review事实；bundle降级不恢复"
            "candidate identity、MTF、history、EV或find_similar门槛。"
            if relaxed_policy else
            (
            '每个open_long/open_short候选还必须先运行只读工具multitimeframe_decision_evidence.py；候选rollout轮必须由candidate_id绑定exact manifest后解析完整instId，旧轮才允许直接传完整instId；本消息已经给出本轮精确临时文件路径，禁止 Get-ChildItem/ls/dir 或以任何方式列举 <PROJECT_ROOT>/tmp；只能读取本轮命令刚生成的具名 out-file。'.replace('<PROJECT_ROOT>', _public_project_path())
            )
        )
    )
    candidate_rollout_active = (
        bundle_phase in {"shadow", "consume"} or candidate_funnel_active)
    candidate_raw_contract_text = (
        "raw必须是JSON object并完整记录candidate_screening、"
        "candidates_deep_dived_v2、candidate_coverage。candidate_screening至少含"
        "phase/pool_count/screened_count/ready_count/briefing_sha256/"
        "bundle_sha256/bundle_path/bundle_status(PASSED|DEGRADED|NOT_APPLICABLE)/gaps；"
        "bundle_status由writer按具名工件独立派生，不得用shadow/consume阶段名代替；"
        "candidates_deep_dived_v2"
        "每项必须从exact manifest原样复制candidate_id，并含symbol/side/layer/"
        "evidence_hash/decision/supporting_evidence(list)/"
        "opposing_evidence(list)/invalidation_condition(非空string或object)/"
        "reason_code/reason_family/reason；Agent输出的reason_code必须先trim并转小写后"
        "完整匹配[a-z0-9][a-z0-9_]{0,63}，只允许小写字母、数字和下划线，"
        "禁止点号、百分号和连字符；数值1.27必须写成1_27。reason_family只允许"
        "MTF_CONFLICT|ENTRY_EXTENDED|LOWER_TIMEFRAME_AGAINST|CATALYST_WEAK|"
        "MICROSTRUCTURE_AGAINST|COST_OR_LIQUIDITY|REPEAT_NO_NEW_EVIDENCE|"
        "DATA_NOT_READY|OTHER_AGENT_JUDGMENT。candidate_id是唯一身份，禁止按简称、"
        "别名或自由文本改写symbol；每个open_long/open_short signal顶层也必须原样写"
        "同一candidate_id，04:00边界后缺失或不一致都会只过滤该OPEN；"
        "reason_family只作诊断，writer会按reason_code/"
        "reason确定性归一并保留你报告的原值，不得把分类差异当作交易门。近6h已淘汰"
        ">=3次时，writer以manifest中的"
        "prior_evidence_hash和本轮evidence_hash确定性判新证据，不再靠一句"
        "new_evidence自证。candidate_coverage至少含"
        "dynamic_floor/dynamic_limit/dynamic_target/actual_count/"
        "dynamic_target_utilization/not_deep_dived_count/stop_reason以及轮换字段。"
        if candidate_rollout_active else ""
    )
    candidate_deep_contract_text = (
        f"{bundle_candidate_count_text}具名候选"
        "全部计入确定性screening；完整深挖只计已读取exact MTF合同且"
        "写入candidates_deep_dived_v2的候选；仅凭 briefing 的涨跌幅、RSI、催化或结构摘要"
        "直接淘汰不算深挖。实际深挖低于dynamic_target时，stop_reason只允许"
        "candidate_shortfall|bundle_degraded|finalize_reserve；达到目标必须写"
        "target_reached，禁止无说明提前停止。每个深挖候选无论保留或淘汰都必须给"
        "正向证据、反向证据、失效条件、candidate_id、evidence_hash、reason_code、"
        "reason_family和reason。只有字段"
        "完整、属于exact briefing池且hash匹配才计质量有效；结构不足不得进入OPEN，但"
        "绝不阻断CLOSE/REDUCE/ADJUST_PROTECTION。writer会由v2确定性投影兼容的"
        "raw.candidates_deep_dived；短缺时同时投影raw.candidate_evidence_shortfall。"
        "signals=[]仍是合法终态，动态目标不是开仓下限，证据下限不是开仓下限。"
        "机会状态只用于排序和审计：ENTRY_READY=严格三周期同向且未延伸，"
        "EXTENDED=严格同向但入场时机延伸，TRIGGERING/EARLY_WATCH=4H已立向而低周期"
        "仍在形成。TRIGGERING/EARLY_WATCH不得仅因其定义内的低周期未完全同向就以"
        "LOWER_TIMEFRAME_AGAINST淘汰；EXTENDED是入场时机反证但不是自动硬门。"
        "四态都不自动授权OPEN，仍须完整证据、成本、风险与确定性执行闸。"
        if candidate_rollout_active else (
            "只要 briefing 的具名成熟/早期 OPEN 候选总数不少于该下限，就必须实际调用 "
            "multitimeframe_decision_evidence.py 至少达到下限；仅凭 briefing 的涨跌幅、"
            "RSI、催化或结构摘要直接淘汰不算深挖。每个已深挖候选无论最终保留或淘汰，"
            "都必须在 raw.candidates_deep_dived 逐项记录完整 instId、evidence_hash、"
            "decision 与 reason；只有 briefing 具名候选总数确实低于下限时才允许少于"
            "下限，并在 raw.candidate_evidence_shortfall 写明 observed_candidate_count "
            "与 reason。signals=[] 仍是合法终态，证据下限不是开仓下限。"
        )
    )
    brief_block = (
        "\n【本轮统一决策简报（分析前预读，直接据此完成分析+实盘）】\n"
        f"--- decision_briefing ---\n{brief}\n--- end ---\n"
    ) if brief else "\n【本轮统一决策简报缺块：按 AGENTS.md 自行补跑 decision_briefing】\n"
    analysis_receipt_contract = (
        "【analysis writer 契约】dispatch_mode=unified 仅表示同一 Agent 承担分析+实盘；"
        "写给 analyst_writer 的 JSON 顶层 mode 必须固定为 full，并包含 "
        "cycle_id/ts/status=ok/decision_protocol=decision_card_v1/regime/"
        "regime_stale/market_summary/missing_sources/signals/raw。"
        "market_summary 必须直接包含 macro/news/tech/sentiment/quant 五个 JSON object；"
        "五段禁止写成 string，建议每段直接用 summary/stance/key_points 结构。"
        "每个 signals[].decision_card 必须直接包含 direction_evidence(list)、"
        "opposing_evidence(list)、execution_conditions、invalidation_point、"
        "risk_reward、portfolio_impact、historical_experience(dict，内含 "
        "matched_wins/matched_losses/missed_opportunities 三个 list 及 usage/reason；"
        "open_* 还必须含 find_similar 原样返回的 evidence_contract)、"
        "agent_judgement、reference_overrides(list)。这些字段不得放在 signal 顶层，"
        "也不得改名为 rationale/final_judgement/overrides；动作允许 "
        "open_long/open_short/hold/close/reduce/adjust_protection/wait，"
        "open_long/open_short 必须显式写 side=long/short 且与 action 一致，"
        "字段枚举契约记为 side=long|short；"
        "不得依赖 writer 兼容归一化；"
        "凡写入 signals 的动作都必须给完整卡，HOLD/WAIT 若写入同样必须给完整卡。"
        "但本自动 unified 路由的 signals 是最终开仓短名单，不是 briefing 逐项转录："
        f"只允许 0..{UNIFIED_OPEN_SIGNAL_CAP} 项且只保留最终决定 open_long/open_short 的"
        "候选；没有最终开仓候选就写 signals=[]。未入选候选不得展开成 WAIT/HOLD signal，"
        "现有持仓也不得在 pre-facts analysis 中逐仓展开成 HOLD signal；全局取舍浓缩进 "
        "market_summary，现仓管理留到同轮 live facts 后逐仓判断。"
        "0..3 只是容量上限，不设最低开仓数、多空配额或强制交易。"
        f"{bundle_contract_text}"
        f"{candidate_raw_contract_text}"
        "open_* 的 risk_reward.exit_mode 必须明确为 "
        "fixed_tp/dynamic_exit/no_fixed_tp；target 仍用于 EV 与参考，只有 fixed_tp 附挂。"
        f"{open_evidence_source_text}"
        "15m/1H/4H 任一 exact 已收盘 K 线、指标或至少 34 根历史不足即不得产出 open。"
        "把工具返回的完整 evidence_contract 原样放进 "
        "decision_card.multitimeframe_analysis.evidence_contract，并对 15m/1H/4H "
        "分别填写 direction/evidence/relative_rank；每个 evidence 固定为非空 "
        "JSON list[string]（一条也写成 [\"...\"]，禁裸字符串/空串/object）；"
        "三个 rank 必须恰为 1/2/3，"
        "selected_timeframe 指向 rank=1 且方向与开仓 side 一致。selection_method 固定为 "
        "relative_rank_1_among_15m_1H_4H_not_calibrated，"
        "calibrated_confidence=null、confidence_claim_allowed=false；"
        "multitimeframe_analysis 本身必须是包含上述全部字段的 JSON dict，不能只放 "
        "evidence_contract，也不能放到 signal 顶层。任一字段无法完整形成时立即淘汰该 "
        "OPEN 候选；若无其它合格候选就写 signals=[]，禁止提交半张 OPEN 卡再靠 writer 报错。"
        "当前前向校准门通过且主人另行风险批准前，不得把 relative rank/"
        "未校准分值写成校准可信度。"
        "每个 open_* 还须把本卡三价原样传给 find_similar_experience.py："
        f"--as-of {cycle_id} --entry <entry> --stop <stop> --target <target>；"
        "禁止自行换算百分比或 RR，工具与 writer 共用规范化函数。"
        "每个拟保留的 OPEN 候选都必须读取上述阶段指定的精确证据源；未读取对应"
        "单币文件或consume bundle项就不得进入signals。把evidence_contract整个"
        "JSON object原样复制进"
        "historical_experience.evidence_contract，禁止编辑、摘录或重算任何键值；尤其不得把 "
        "query.as_of/instrument_context.as_of 的空格分隔时间改成 cycle_id 的 T 分隔格式，"
        "这些字段参与 evidence_hash，改一个字符就会拒写。historical_experience.reason、"
        "direction_evidence、opposing_evidence 与 agent_judgement 禁止手写样本 n、W/L、WR 或 "
        "胜率；计数只允许由 writer 从契约注入 scope_counts。无法满足就淘汰候选，不得猜数。"
    )
    zero_open_writer_contract = (
        "【零开仓双writer不变量】主动淘汰全部 OPEN 候选只是分析结论，不是 "
        "abort/skipped/stale，也不是 analysis 失败。只要 gate=ok，即使最终零开仓、"
        "无持仓，也必须写完整 mode=full,status=ok,signals=[] 的 analysis 回执，依次通过 "
        "validate-only 与正式 analyst_writer；随后读取 live facts，写 actions=[] 的完整 "
        "position plan，并由 live_position_action_runner 提交 HOLD trade_cycles。"
        "上述两个 writer 都成功前禁止最终答复或 stop。只有 gate 真实 abort/stale、"
        "analysis writer 实际失败或 writer 返回 analysis 非 ok，才允许在 facts 前停止。"
        "严禁把‘准备写 analysis’‘接下来调用 writer’‘将生成完整 JSON’等过渡文字当作回复；"
        "候选取舍完成后的下一次响应必须直接是最终 analysis 文件的 write 工具调用。"
    )
    trade_receipt_contract = (
        ("【trade writer 契约】交易阶段的 cycle 顶层 decision_card 不是摘要容器，"
        "必须直接包含同一组固定键：direction_evidence(list)、opposing_evidence(list)、"
        "execution_conditions、invalidation_point、risk_reward、portfolio_impact、"
        "historical_experience(dict)、agent_judgement、reference_overrides(list)。"
        "HOLD/WAIT/REDUCE/ADJUST_PROTECTION 也必须完整填写，禁止改成 "
        "summary/open_candidates/hold_positions。"
        "只有 executor 成功返回 action_taken=ADJUST_PROTECTION 且保留 "
        "protection_change、path、protection_state.ok=true 与 applied，才可报告保护调整；"
        "未调用 executor 或无终局回读时必须写 HOLD，不得自行写模糊 ADJUST。"
        "【逐仓显式复核格式】这不是新增统计口径，而是把 AGENTS.md 的既有要求前置："
        "每个当前持仓必须在 agent_judgement 单独写一个分句，完整 instId 与最终 "
        "HOLD|CLOSE|REDUCE|ADJUST_PROTECTION 必须出现在同一分句，并紧接具体理由；"
        "完整 instId 与动作词之间不得插入逗号、句号或分号，否则严格审计无法把结论"
        "确定性归到该仓。actions=[] 的 HOLD 同样适用；不得靠放宽审计或事后重判补救。"
        "OPEN/ADD 必须原样保留 analysis 的 multitimeframe_analysis；executor 会在任何"
        "交易所账户/订单 I/O 前按同 cycle 重读 market.db，当前三周期未就绪则 clean reject。"
        "完全一致使用 current_market_exact；若同槽后续采集修订已收盘数据，仅当卡内契约"
        "逐字段命中同 cycle/symbol/side 的 analysis.db writer 已验证锚点"
        "analysis_db_writer_validated 才继续，并在回执保留 post_analysis_market_revision"
        "和 supplied/current/persisted hash；否则 clean reject。持久化锚点不替代 readiness。"
        "调用 trades_writer.py --facts-file 时，回执应省略 live_facts 让 writer 原样注入；"
        "若携带则必须与 facts 文件整份完全相同，禁止摘要或重算。"
        "本轮不论是否包含 OPEN/ADD，都必须一次 write 完整 position plan 后立即且只调用一次 "
        "live_position_action_runner.py；actions=[] 即 HOLD，OPEN/ADD/CLOSE/REDUCE/"
        "ADJUST_PROTECTION 可在同一 plan 混合。runner 不替你判断，也不设退出阈值；OPEN/ADD"
        "只写 side/target_stop_risk_pct_equity/lev，runner 从 analysis.db 只读绑定同 symbol"
        " canonical card 并确定性换算张数。计划 receipt_context 至少放 cycle/status/protocol/"
        "regime，禁止预填 decision/action_taken/n_orders/trades/errors/ok，也禁止重抄或四舍五入 "
        "equity；runner 必须从同轮 live_facts.balance.totalEq 注入唯一权威值。actions 中有 "
        "OPEN/ADD 时，不得在 receipt_context 手抄或缩写 decision_card：runner 会在首次上下文"
        "校验前以首个新风险动作的 analysis.db writer 验证卡为基底，仅保留 Agent 周期卡里的 "
        "agent_judgement/position_reviews，各动作仍逐标的重读 canonical card 并通过全部原有硬闸；"
        "纯持仓/HOLD plan 则仍须提供完整周期 decision_card。"
        "【HOLD 卡硬约束】该 decision_card 必须是完整 dict（禁止 null/字符串/省略）；"
        "direction_evidence 与 opposing_evidence 必须都是非空 list——零开仓轮也要写"
        "『支持维持现状』与『可能促使动作』双向证据各≥1条；historical_experience.usage "
        "只能取 adopt|partial|ignore|none 且 reason 非空，HOLD 卡不携带 "
        "evidence_contract（那是 open 卡 find_similar 的产物）；risk_reward 必须是"
        "非空 dict 且顶层平铺（禁止按 symbol 嵌套子对象），exit_mode 要么整个省略、"
        "要么取 fixed_tp|dynamic_exit|no_fixed_tp，禁止自造 no_new_risk/"
        "no_action_this_cycle 等枚举外值；reference_overrides 无覆盖时填 []。"
        f"plan=<PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json，"
        f"receipt=<PROJECT_ROOT>/tmp/_receipt_live_{safe_cycle}.json。"
        "命令：pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 "
        "<PROJECT_ROOT>/scripts/live_position_action_runner.py "
        f"--cycle-id {cycle_id} --plan-file <PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json "
        f"--facts-file <PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json "
        f"--receipt-file <PROJECT_ROOT>/tmp/_receipt_live_{safe_cycle}.json --db-root <PROJECT_ROOT>/db。"
        "write plan 工具返回后，下一条工具调用必须直接执行上述 runner 命令；中间禁止回复文字、"
        "最终答复、read/edit/query 或任何其他工具。plan 落盘不等于业务完成。"
        "若完整 plan 已落盘 10 秒仍无合法 marker，stage supervisor 会确定性启动同一个 "
        "runner；你仍照常立即调用，原 profile 锁与 handoff CAS 保证竞态只有一个执行。"
        "晚到调用若只返回并发锁或已有 started/executing/committed，不得重跑、改 plan 或宣称"
        "业务失败，终态由 stage supervisor 独立核验；failed_preflight 则按下述唯一重写处理。"
        "plan 后立即 runner/30s 机器闸核验 live_runner_state 的 cycle/facts_hash/"
        "plan_sha256。runner 在后续 OPEN/ADD 前会先提交此前成交、显式带 "
        "runner_in_progress=true 的 partial superset；batch_status=partial|failed、"
        "interim/final writer 失败或非竞态 runner 非零退出就是 terminal failure，禁止重跑、补动作或另写 HOLD。"
        "唯一例外：runner 预检拒（stdout error_kind=plan_preflight_failed 且 "
        "live_runner_state.state=failed_preflight，交易所零副作用）允许且只允许"
        "raw SHA 已变化的整文件重写 plan 一次并立即重调同一 runner 命令，沿用同轮 facts；"
        "同 plan 晚到调用不消耗重写额度；预检拒后 "
        "180 秒内未完成重写即按终态收口；第二次预检拒即 terminal failure，不得再改写。").replace('<PROJECT_ROOT>', _public_project_path())
    )
    if relaxed_policy:
        analysis_receipt_contract = (
            "【主人批准的全市场轻量OPEN合同】policy=all_market_lightweight_open_v1；"
            "analysis仍固定mode=full,status=ok、"
            "decision_protocol=decision_card_v1并保留五段market_summary。全量manifest没有"
            "成交额/OI/4H立向/8+8/16项准入限制；Agent对有界decision slice逐项决策。"
            "最终signals没有数字上限，也没有最低交易数。每个OPEN只需symbol、action、side、"
            "非空reasoning、正有限entry_hint/stop_hint/tp_hint及可选exit_mode；writer保存"
            "contract=lightweight_open_v1。candidate_id、reason_family、历史经验/EV、"
            "multitimeframe_analysis及evidence hash均不得作为OPEN准入或writer条件。"
            "四态ENTRY_READY/EXTENDED/TRIGGERING/EARLY_WATCH均默认进入provisional_open："
            "若不形成同symbol/side OPEN，"
            "raw.candidates_deep_dived_v2必须给primary_disqualifier={kind,metric,"
            "observed_value,operator,boundary_value,source_path}；kind只允许"
            "cost_or_liquidity|microstructure|invalidation|cost_adjusted_ev。"
            "缺催化、重复出现、状态名、任意周期K线成交量/成交额、OI或纯主观判断"
            "不能单独否决任何四态；流动性veto只认可复算spread/slippage/orderbook depth。"
            "只有本轮无四态review候选，或所有已review四态候选均有可复算主要否决项时，"
            "signals=[]才可通过writer。"
        )
        trade_receipt_contract = (
            ("【轻量OPEN交易合同】OPEN/ADD plan仍只允许side、"
            "target_stop_risk_pct_equity与lev；runner必须从本cycle analysis_signals重读"
            "canonical symbol/action/side和lightweight_open_v1卡。executor不再要求或复验"
            "MTF/history/EV/candidate identity，但账户求真、账仓一致、intent幂等、SL、"
            "10x、15%单笔保证金、5%止损风险、98%可用保证金、1%名义、66.6%组合IMR、"
            "成交与保护回读全部保持。HOLD与退出合同不变；HOLD必须把完整卡放在"
            "receipt_context.decision_card，并使用decision_protocol准确字段名，禁止protocol别名"
            "或plan顶层decision_card。analysis writer成功后先生成并读取live facts；若"
            "facts.positions非空，必须在write plan之前单独运行：pwsh -NoProfile -File "
            "<PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/"
            "multitimeframe_decision_evidence.py --db-root <PROJECT_ROOT>/db "
            f"--facts-file <PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json --cycle-id {cycle_id} "
            f"--out-file <PROJECT_ROOT>/tmp/position_exit_{safe_cycle}.json "
            f"--decision-view-file <PROJECT_ROOT>/tmp/position_exit_view_{safe_cycle}.json。"
            "工具成功后只读取精简decision-view；完整position_exit由runner做hash/facts绑定，"
            "禁止把完整大文件加载进模型上下文。decision-view中每个现仓必须在plan周期卡"
            "给出HOLD/CLOSE/REDUCE/ADJUST_PROTECTION结论；view的source_status必须为"
            "PASSED、facts_hash与本轮facts一致、position_count与现仓一致，否则停止；"
            "view完成后120秒内write plan。"
            "OPEN取消MTF合同仅指新增仓，不取消既有持仓的逐仓退出/保护证据；该工具只读、"
            "不下单。facts.positions为空时不得伪造该文件。").replace('<PROJECT_ROOT>', _public_project_path())
        )
        zero_open_writer_contract = (
            "【零OPEN可审计条件】零OPEN仍须完成analysis与HOLD双writer，但不能以空数组"
            "规避四态默认授权；writer会拒绝缺OPEN且缺结构化主要否决项的回执。"
        )
    if minimal_policy:
        analysis_receipt_contract = (
            "【主人批准的无三周期/无六项卡合同】"
            "policy=no_three_period_no_six_card_v1；analysis固定mode=full,status=ok、"
            "decision_protocol=minimal_decision_v2。全市场manifest每个symbol只出现一次，"
            "side=null、eligible_sides=[long,short]；不得读取或判断15m/1H/4H，不得输出"
            "四态、trend_strength、entry_timing或MTF理由。Agent只读side-neutral decision "
            "slice，对每个review symbol选择long|short并写轻量OPEN，或直接reject。"
            "顶层signals必须是JSON list，禁止包成signals.analysis_signals；market_summary"
            "必须恰含macro/news/tech/sentiment/quant五个object。"
            "raw.candidates_deep_dived_v2每项只需symbol、decision、reason；decision只写"
            "provisional_open或reject，禁止veto/open_long/open_short别名。"
            "candidate_coverage必须放在raw内，dynamic_limit写本轮动态上限"
            f"{dynamic_candidate_limit}，不得写全量manifest数；OPEN项的side只由对应"
            "open_long|open_short "
            "signal确定。OPEN signal仍只需symbol/action/side/reasoning/entry_hint/stop_hint/"
            "tp_hint/exit_mode；writer内部保存lightweight_open_v1机器执行包。"
            "禁止candidate identity、evidence hash、三周期、history、EV或六项卡成为准入。"
        )
        trade_receipt_contract = (
            ("【最小交易合同】analysis成功后只生成并读取一次live facts。现仓证据仍调用"
            "既有facts-file批处理以绑定cycle/facts_hash/全部symbol+side/开仓路径/当前PnL/"
            "SL/protection floor，但新策略输出不含任何15m/1H/4H字段，PASSED不依赖K线。"
            f"命令：pwsh -NoProfile -File <PROJECT_ROOT>/scripts/run_okx_python.ps1 "
            f"<PROJECT_ROOT>/scripts/multitimeframe_decision_evidence.py --db-root <PROJECT_ROOT>/db "
            f"--facts-file <PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json --cycle-id {cycle_id} "
            f"--out-file <PROJECT_ROOT>/tmp/position_exit_{safe_cycle}.json "
            f"--decision-view-file <PROJECT_ROOT>/tmp/position_exit_view_{safe_cycle}.json。"
            "plan.receipt_context只允许cycle_id/mode/status/regime/"
            "decision_protocol=minimal_decision_v2/reasoning/position_reviews；"
            "禁止decision_card、direction_evidence、opposing_evidence、execution_conditions、"
            "invalidation_point、risk_reward、portfolio_impact、historical_experience与"
            "reference_overrides。OPEN/ADD action仍由runner从analysis_signals读取轻量机器"
            "执行包获取SL/TP；HOLD/退出无卡。账户、账仓、intent、10x、15%、5%、98%、"
            "1%、66.6%、成交及保护回读保持。plan固定为"
            f"<PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json；runner命令：pwsh -NoProfile -File "
            "<PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/live_position_action_runner.py "
            f"--cycle-id {cycle_id} --plan-file <PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json "
            f"--facts-file <PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json --receipt-file "
            f"<PROJECT_ROOT>/tmp/_receipt_live_{safe_cycle}.json --db-root <PROJECT_ROOT>/db。").replace('<PROJECT_ROOT>', _public_project_path())
        )
        zero_open_writer_contract = (
            "【最小双writer】无论是否有动作都完成analysis writer、facts、position evidence、"
            "一个plan和runner；HOLD只写reasoning/position_reviews，不得补六项卡。"
        )
    slot_priority_text = (
        "本轮为整点槽：先完成 fast/slow/regime 采集闸与全部持仓复核，"
        "再用真实剩余预算做候选深挖；持仓复核不得为候选数量让路。"
        if str(cycle_id).endswith(":00")
        else "本轮为 15 分钟槽：fast 闸完成后，用真实剩余预算扩大完整证据覆盖"
        "与判断深度。"
    ) + (
        "扩大分析范围不是交易配额；剩余预算不足以完整收口"
        "（含 facts、逐仓判断、executor、writer）时，不得启动新的重型候选深挖。"
        "【轮换】候选充足时，本轮深挖集合至少包含 1 个 briefing 标注"
        "『近6h未深挖』的候选（简报附轮换菜单）；无此类候选则按排序取舍。"
        "同一标的近 6h 已被淘汰 ≥3 次时应降权，再挖须在该候选 reason 首句注明新证据。"
    )
    throughput_contract = (
        f"【周期内吞吐契约】统一轮的业务完成目标为起棒后 "
        f"{analysis_target_min + finalize_reserve_min} 分钟内形成成功终态："
        f"前 {analysis_target_min} 分钟内冻结 analysis，至少预留 "
        f"{finalize_reserve_min} 分钟完成 live facts、持仓管理/交易、writer 与"
        "只读终态核验。"
        f"{slot_priority_text}"
        "优先直接消费已预读 briefing，open 候选深挖的动态目标区间为 "
        f"{UNIFIED_OPEN_REVIEW_MIN}..{UNIFIED_OPEN_FINALISTS} 个：候选充足时先完整检查前 "
        f"{UNIFIED_OPEN_REVIEW_MIN} 个，如因数据不就绪或 Agent 判断否决，就在同一分析预算内"
        f"依次顺延至第 4 个及其后，上限 {UNIFIED_OPEN_FINALISTS} 个。现有持仓数量、"
        "已持有同向/反向仓、软集中度或未触发硬风控的组合占用，不得单独成为停止候选求证的"
        "理由。到分析预算仍无可辩护开仓时，立即写完整 "
        "market_summary 与 signals=[]，而不是继续搜索或生成候选 WAIT/HOLD 卡。随后交易阶段"
        f"仍以完整顶层 HOLD/WAIT 回执记录无动作结论。深挖 "
        f"{UNIFIED_OPEN_REVIEW_MIN}..{UNIFIED_OPEN_FINALISTS} 不是强制交易，最终开仓卡可为 0..3，"
        "不设最低数和方向配额。该预算只消除串行探查和收尾拖延，"
        "不限制做多、做空、"
        "全平、减仓、止损或止盈裁决，也不得用来跳过 gate、证据契约、executor、writer、"
        "保护单确认或失败重写规则。分析落库后立即进入 facts；trades_writer 返回 ok:true 后立即给出"
        "简短终答，不再复盘、扩展研究或调用任何工具；trade_cycles 的独立落库后置核验只由"
        "stage_runner 承担。"
        "【本轮动态时间预算】本消息生成时刻为 "
        f"{observed_at_text}，距 {analysis_budget_label}尚余 "
        f"{analysis_remaining_seconds} 秒{candidate_budget_note}；"
        "本轮候选深挖区间据此收敛为 "
        f"{dynamic_candidate_floor}..{dynamic_candidate_limit} 个、硬上限="
        f"{dynamic_candidate_limit}；MTF就绪约束后的本轮动态目标="
        f"{dynamic_candidate_target}、动态下限={dynamic_candidate_floor}；"
        f"本轮证据深挖下限={dynamic_candidate_floor}。"
        f"{candidate_deep_contract_text}"
        "这是迟起小时轮的耗时预算，不是方向、开仓数或仓位"
        "约束；完整 300+ 宇宙判断仍由已预读 briefing/确定性快照承担。达到本轮上限后"
        "立即完成取舍，禁止再开新候选。消息已给出权威调度时钟和完整 writer 契约；"
        "自动轮禁止调用 session_status，禁止读取 analysis_template、"
        "trade_template、writer 源码或无关手册。允许读取 MEMORY.md 并调用 memory_search"
        "（两者合计≤2 次，建议用于最终 open/风险动作候选的同标的历史教训）；"
        "记忆命中与本消息、briefing 或权威事实脚本冲突时一律以后者为准，"
        "不得因记忆检索逼近本轮时间闸。只允许为最终 open 候选补齐必需的三周期/"
        "经验契约、上述受限记忆检索，以及随后 facts、executor、writer 所需工具。"
        f"{time_gate_text}临近 {analysis_budget_label}时不再开新的"
        "候选深挖，立即完成已有证据的取舍与 writer；这不允许跳过风控、executor、成交确认或终态落库。"
        "到达 Agent 业务终态硬截止后不得再发起新的 OPEN/ADD/CLOSE/REDUCE 或独立保护调整；"
        "runner 会先经本轮原 Gateway 连接取消 active run，再以隔离 session 复核终态；"
        "executor 仍按 cycle 二次拒绝后台残留 turn；"
        "已开始订单的保护确认与安全回滚仍须完整收尾。"
    )
    if relaxed_policy:
        throughput_contract = (
            f"【全市场轻量策略吞吐】本轮业务终态仍严格早于{terminal_deadline_text}；"
            f"全量manifest={manifest_count}，Agent只读"
            f"{'decision slice='+str(decision_slice_path) if bundle_healthy else '消息内预读briefing review fallback'}，"
            f"动态完整review目标={dynamic_candidate_target}、上限={dynamic_candidate_limit}。"
            "review上限只是串行判断预算，不是manifest、方向或最终OPEN数量上限。"
            "四态候选均先走provisional_open或可复算veto；通过者全部进入signals，不设0..3。"
            "自动Live轮禁止读取MEMORY.md、DREAMS.md及memory目录，禁止memory_search；"
            "OPEN也不调用MTF/find_similar合同工具。到时限立即完成已有slice的结构化取舍，"
            "仍不得跳过facts、runner、risk_validator、executor、writer、成交及保护确认。"
        )
    if minimal_policy:
        throughput_contract = (
            f"【无三周期最小吞吐】本轮业务终态仍严格早于{terminal_deadline_text}；"
            f"全量side-neutral manifest={manifest_count}，只读"
            f"{'decision slice='+str(decision_slice_path) if bundle_healthy else '消息内预读review fallback'}；"
            f"动态review目标={dynamic_candidate_target}、上限={dynamic_candidate_limit}。"
            "slice采用全市场round-robin，不按方向、四态、周期或成熟/早期排序。"
            "每个review symbol只需OPEN或reject并说明reason；OPEN方向由Agent选择。"
            "analysis后禁止--help、源码、旧plan、历史回执与额外查询；只允许facts、"
            "无周期position evidence、write plan、runner。真钱硬闸与失败关闭保持。"
        )
    terminal_contract = (
        "【交易阶段终止契约】live facts 文件读完后，writer 命令、回执字段与流程已由本消息"
        "和 AGENTS.md 完整给出；禁止再读取 trades_writer.py 源码、探查 schema、搜索或读取"
        "历史 _receipt_live_*.json、回看无关手册"
        "或继续研究实现。必须立即形成最终交易判断：若不 "
        "OPEN/ADD/CLOSE/REDUCE/ADJUST_PROTECTION 就生成完整 HOLD/WAIT 回执；"
        "随后调用 trades_writer；writer 返回前禁止最终答复、禁止无内容 stop。writer 返回 ok:true 后，"
        "严禁再调用 query_db、--help、--schema 或"
        "任何其他工具，必须立即发送简短最终答复；stage_runner 会独立核验本 cycle 的 trade_cycles，"
        "Agent 不得重复核验。即使零成交，HOLD 也必须先落库且只能由 writer 完成；writer 失败则按手册保留"
        "文件并报告 terminal failure，不得把未落库当作正常结束。"
    )
    return (
        f"OKX 本轮工作：stage=live cycle={cycle_id} dispatch_mode=unified；"
        "analysis_receipt_mode=full。"
        f"你是本轮唯一分析+实盘决策 Agent：先以 cycle={cycle_id} 执行采集 gate，"
        f"生成并经 analyst_writer 落 analysis.db；仅 status=ok 且 writer 成功后，"
        f"再直查 OKX live 现仓/余额，完成实盘决策、executor 调用和 trades_writer 落库。"
        f"{analysis_receipt_contract}{zero_open_writer_contract}"
        f"{trade_receipt_contract}{throughput_contract}"
        f"{terminal_contract}"
        f"{candidate_tool_instruction}"
        "analysis 回执必须直接用 write 一次完整写最终 JSON 文件，禁止先建 Python/PowerShell"
        "生成器，先 validate-only 后正式 writer，禁止 edit/局部补丁循环；validate-only 返回 "
        "ok:true 后，下一条工具调用必须直接用完全同一文件执行正式 analyst_writer，中间禁止"
        "回复文字、终答、read/edit/query 或其它工具；validate-only 不等于 analysis 已落库。"
        "校验失败最多用 write"
        "整文件覆盖一次，第二次失败即停止，禁止跳过 writer。"
        "禁止调用 sqlite3 CLI 临时探查 schema 或业务库；schema 只读 db/schema.sql，"
        "业务查询只用已批准的 query_db.py/事实脚本。"
        "gate/两个 writer/executor 的 cycle_id 均固定为上述派单 cycle，禁墙钟重解析。"
        "不得等待或调用 push；dispatcher 会在 analysis/live 落库后接力。"
        f"按你的 AGENTS.md（操作手册）执行。{brief_block}"
    )


def _send_autoheal_p0_alert(profile: str, cycle_id: str,
                            findings: list[dict]) -> bool:
    """Best-effort private P0 alert; delivery failure never clears the block."""
    kinds = sorted({str(item.get("kind") or "UNKNOWN") for item in findings})
    locations = sorted({
        f"{item.get('symbol')}/{item.get('side')}"
        for item in findings if item.get("symbol") and item.get("side")
    })
    # kind 码是 findings/指纹契约，展示层只加中文括注；指纹仍用原始 kinds。
    kinds_disp = ",".join(zh_labels.autoheal_kind_gloss(k) for k in kinds[:5])
    message = compact_text(
        f"[P0] {profile} 账本自愈阻断交易起棒；cycle={cycle_id}；"
        f"问题={kinds_disp}；"
        f"位置={','.join(locations[:5]) or '未提供标的'}。"
        "请人工核验交易所保护单与账本，系统未下单。",
        560,
    )
    fingerprint = hashlib.sha256(json.dumps(
        {"profile": profile, "cycle": cycle_id,
         "kinds": kinds, "locations": locations},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()[:16]
    push_py = Path(__file__).parent.parent / "scripts" / "qq_push.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(push_py), "--alert", "--message", message,
             "--dedupe-key", f"autoheal-p0:{profile}:{cycle_id}:{fingerprint}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # qq_push 内部总预算封顶 55s；外层 60s 保留状态收尾时间。
            timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _check_tmp_stdlib_shadow(stage: str, cycle_id: str) -> list[str]:
    """插入点 A0：起棒前查 tmp 有没有文件遮蔽标准库（2026-08-06）。

    trader 的当轮执行脚本按契约只能写 `<PROJECT_ROOT>/tmp/`，Python 会把该目录放进
    `sys.path[0]`；tmp 里一旦有 `bisect.py` 之类调试残留，`order_executor` 的
    `import tempfile` 会直接 ImportError，**下一笔 OPEN/CLOSE 必炸**。

    **只告警不阻断**（同 2026-08-05 autoheal 拍板的边界）：HOLD 轮的回执走
    `collectors/trades_writer.py`（sys.path[0] 是 collectors/），不受影响；
    在派发层阻断会把本来能正常完成的 HOLD 轮一起杀掉，比问题本身更糟。
    """
    try:
        if _public_project_path('scripts') not in sys.path:
            sys.path.insert(0, _public_project_path('scripts'))
        from tmp_cleanup import find_stdlib_shadows

        names = [p.name for p in find_stdlib_shadows(Path(_public_project_path('tmp')))]
    except Exception as exc:            # 探测本身绝不拖垮起棒
        print(f"[trigger] WARN tmp 遮蔽探测失败（忽略）: {exc}", file=sys.stderr)
        return []
    if not names:
        return []
    print(f"[trigger] WARN tmp 下有文件遮蔽标准库 stage={stage} "
          f"cycle={cycle_id} files={','.join(names)}"
          "——本轮若产生成交，执行脚本会炸在 import；不阻断起棒",
          file=sys.stderr)
    _send_tmp_shadow_alert(stage, cycle_id, names)
    return names


def _send_tmp_shadow_alert(stage: str, cycle_id: str, names: list[str]) -> bool:
    """按遮蔽文件名集合去重的 P1 告警——同一批残留只吵一次，不是每轮一条。"""
    message = compact_text(
        (f"[P1] <PROJECT_ROOT>/tmp 下有 {len(names)} 个文件遮蔽标准库："
        f"{','.join(sorted(names)[:5])}；stage={stage} cycle={cycle_id}。"
        "trader 当轮执行脚本写在 tmp、sys.path[0] 即该目录，下一笔 OPEN/CLOSE "
        "会 ImportError（HOLD 轮不受影响）。处理：删除或改名这些文件，"
        "或跑 tmp_cleanup.py --apply。").replace('<PROJECT_ROOT>', _public_project_path()),
        480,
    )
    fingerprint = hashlib.sha256(
        json.dumps(sorted(names), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    push_py = Path(__file__).parent.parent / "scripts" / "qq_push.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(push_py), "--alert", "--message", message,
             "--dedupe-key", f"tmp-stdlib-shadow:{fingerprint}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            # qq_push 内部总预算封顶 55s；外层 60s 保留状态收尾时间。
            timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _autoheal_ledger(
    stage: str,
    cycle_id: str,
    *,
    apply_enabled_override: bool | None = None,
) -> dict:
    from scripts.ledger_recovery import enabled, recover_in_budget
    if stage != "live" or not enabled(cycle_id):
        return _autoheal_ledger_once(
            stage, cycle_id, apply_enabled_override=apply_enabled_override)
    return recover_in_budget(
        lambda budget: _autoheal_ledger_once(
            stage, cycle_id, apply_enabled_override=apply_enabled_override,
            timeout_sec=budget),
        verify_once=lambda budget: _autoheal_ledger_once(
            stage, cycle_id, apply_enabled_override=False, timeout_sec=budget),
        cycle=cycle_id, timeout_sec=180)


def _autoheal_ledger_once(
    stage: str,
    cycle_id: str,
    *,
    apply_enabled_override: bool | None = None,
    timeout_sec: float | None = None,
) -> dict:
    """插入点 A：起棒前检查并自愈账本（2026-08-04；补 close 08-05、严格 T1 补 open 09-11 起默认开）。

    必须跑在 `_run_briefing()` **之前**——简报里的持仓视图喂给 Agent 决策，
    幽灵仓会让 Agent 基于「不存在的持仓」做判断（2026-08-04 SKHY 事故即如此），
    同时也早于 `order_executor` 的 pretrade 闸，顺带消除拒单冻结。

    确定性子进程、零 LLM。只有 v1 契约 rc=0（干净或安全写入完成）
    才能继续起棒；未解决、错误、跳过、P0 以及缺失/损坏/过期契约均阻断。
    `--self-cycle` 让本 stage 自己的 running runner 不被当成互斥冲突。
    """
    profile = stage if stage == "live" else None
    if not profile:
        return _autoheal_client_result(
            None, cycle_id, "not-applicable", status="not_applicable", rc=0,
            reason="stage does not use a trading ledger",
            finding_kind=None,
        )
    request_id = uuid.uuid4().hex
    resolved_db_root = _DB_ROOT.resolve()
    if timeout_sec is not None and timeout_sec <= 0:
        return _autoheal_client_result(
            profile, cycle_id, request_id, status="client_error", rc=2,
            reason="recovery_budget_exhausted", db_root=resolved_db_root)
    if os.environ.get("OKX_DISABLE_LEDGER_AUTOHEAL") == "1":
        return _autoheal_client_result(
            profile, cycle_id, request_id, status="disabled", rc=0,
            reason="OKX_DISABLE_LEDGER_AUTOHEAL=1", db_root=resolved_db_root,
            finding_kind=None,
        )
    try:
        heal_py = Path(__file__).parent.parent / "scripts" / "ledger_autoheal.py"
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        out_json = LOG_DIR / (
            f"autoheal-{profile}-{_safe_cycle(cycle_id)}-{request_id}.json")
        out_json.unlink(missing_ok=True)
        cmd = [sys.executable, str(heal_py), "--profile", profile,
               "--db-root", str(resolved_db_root),
               "--self-cycle", cycle_id, "--request-id", request_id,
               "--json-out", str(out_json)]
        # Public release: inspection only, including legacy environment overrides.
        apply_enabled = False
        unrecorded_enabled = False
        if apply_enabled:
            cmd.append("--apply")
        if unrecorded_enabled:
            cmd.append("--enable-unrecorded")
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=(180 if timeout_sec is None else timeout_sec),
            creationflags=_CREATE_NO_WINDOW,
        )
        result = _read_autoheal_contract(
            out_json, request_id=request_id, profile=profile,
            cycle_id=cycle_id, db_root=resolved_db_root,
            returncode=proc.returncode,
        )
    except Exception as exc:
        result = _autoheal_client_result(
            profile, cycle_id, request_id, status="client_error", rc=2,
            reason=f"{type(exc).__name__}: {exc}", db_root=resolved_db_root,
        )
    if result.get("p0"):
        p0_findings = [
            item for item in result.get("findings", [])
            if isinstance(item, dict)
            and str(item.get("sev") or "").upper() == "P0"
        ]
        result["alerted"] = _send_autoheal_p0_alert(
            profile, cycle_id, p0_findings)
        result["p0_kinds"] = sorted({
            str(item.get("kind") or "UNKNOWN") for item in p0_findings
        })
    result["json_out"] = str(out_json) if "out_json" in locals() else None
    return result


def _analyst_briefing(
    cycle_id: str,
    *,
    candidate_out_file: Path | None = None,
    ready_pool_out_file: Path | None = None,
) -> str:
    """为 analyst 预读 decision_briefing 塞进 fire 消息，省去 analyst 临场摸库（降时延）。"""
    return _run_briefing(
        cycle_id,
        candidate_out_file=candidate_out_file,
        ready_pool_out_file=ready_pool_out_file,
    )


def _briefing_for_traders(cycle_id: str) -> str:
    """trader 预载简报——每 cycle 只真跑一次，走文件缓存（
    第二棒直接读缓存，避免 2×60s 最坏）。

    与 analyst 预载**刻意不共缓存**：analyst 简报生成于分析之前（无本轮 signals）；
    trader 派发时本轮 analysis 已落库，预载决策卡与历史正反样本，减少重复摸库
    。缓存 logs/trigger/briefing-<cycle>-trader.txt，
    随 log_rotate 每日轮转回收。全程 fail-safe：缓存读写失败照常直跑/直用。"""
    cache = LOG_DIR / f"briefing-{_safe_cycle(cycle_id)}-trader.txt"
    try:
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_text(encoding="utf-8")
    except OSError:
        pass
    brief = _run_briefing(cycle_id)
    if brief:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            cache.write_text(brief, encoding="utf-8", newline="\n")
        except OSError:
            pass
    return brief


def _ro_db(name: str) -> sqlite3.Connection | None:
    p = _DB_ROOT / name
    if not p.exists():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con




# `_demo_swap_pool()` 随 2026-08-06 demo 全量下线移除：它整池取 Demo 环境 SWAP
# 合约供预载标注「池内有无」，只服务已删除的 ③.5 合约可用性块。


def _trader_preload(cycle_id: str, stage: str) -> str:
    """live 触发消息预载块。

    dispatcher 起 trader 前已核过 analysis 就绪/新鲜/status=ok——把已证事实与分析内容
    直接塞进触发消息，消 trader 冷启动逐库自查（demo 失败调用 2.6x 于 live 的主源=
    开场摸库列名瞎猜）。每块独立 fail-safe：取不到→留显式缺块标记，trader 按 AGENTS.md
    自取兜底，绝不阻断派发。**OKX API 现仓/余额刻意不预载**——现仓唯一权威=交易所 API
    且随时变（SL 成交/手动平仓），预载快照会诱导 agent 跳过 API 真查，而
    risk_validator 的 open_positions 必须是下单现场真值（红线）。"""
    parts: list[str] = []
    # ①② 派发确认 + 分析预读（analysis.db ro；dispatcher 刚核过 status=ok 才会走到这）
    try:
        con = _ro_db("analysis.db")
        run = con.execute(
            "SELECT ts, status, mode, regime, regime_stale, market_summary, "
            "missing_sources FROM analysis_runs WHERE cycle_id=?",
            (cycle_id,)).fetchone()
        sigs = con.execute(
            "SELECT symbol, total, action, side, confidence, entry_hint, stop_hint, "
            "tp_hint, reasoning, decision_card FROM analysis_signals WHERE cycle_id=? "
            "ORDER BY CASE action WHEN 'close' THEN 0 WHEN 'reduce' THEN 0 "
            "WHEN 'adjust_protection' THEN 0 WHEN 'open_long' THEN 1 "
            "WHEN 'open_short' THEN 1 WHEN 'hold' THEN 2 ELSE 3 END, rowid",
            (cycle_id,)).fetchall()
        con.close()
        if run is None:
            raise LookupError("no analysis row")
        parts.append(
            "【本轮派发确认（dispatcher 已核，勿再查派发/分析就绪性）】\n"
            f"analysis: status={run['status']} mode={run['mode']} ts={run['ts']} "
            f"regime={run['regime']} regime_stale={run['regime_stale']} "
            f"missing_sources={run['missing_sources'] or '无'}")
        lines = []
        for s in sigs:
            rs = str(s["reasoning"] or "").replace("\n", " ")[:200]
            try:
                card = json.loads(s["decision_card"]) if s["decision_card"] else None
            except (json.JSONDecodeError, TypeError):
                card = None
            if isinstance(card, dict):
                hist = card.get("historical_experience") or {}
                lines.append(
                    f"  {s['symbol']} action={s['action']} side={s['side'] or '-'} "
                    f"entry={s['entry_hint'] or '-'} stop={s['stop_hint'] or '-'} "
                    f"tp={s['tp_hint'] or '-'}\n"
                    f"    方向={compact_text(card.get('direction_evidence'), 150)}\n"
                    f"    反对={compact_text(card.get('opposing_evidence'), 150)}\n"
                    f"    裁决={compact_text(card.get('agent_judgement'), 180)}\n"
                    f"    历史={hist.get('usage', 'none')}:"
                    f"{compact_text(hist.get('reason'), 120)}"
                )
            else:
                # 兼容格式仅展示文字理由，不把评分解释为当前协议依据。
                lines.append(
                    f"  {s['symbol']} action={s['action']} side={s['side'] or '-'} "
                    f"entry={s['entry_hint'] or '-'} stop={s['stop_hint'] or '-'} "
                    f"tp={s['tp_hint'] or '-'} | 兼容格式: {rs}")
        ms = str(run["market_summary"] or "")[:2500]
        parts.append(
            "【本轮分析预读（analysis.db 已读，勿再查）】\n"
            f"signals（{len(sigs)} 行，决策卡顺序）:\n"
            + ("\n".join(lines) if lines else "  （空=本轮无信号，全 hold）")
            + (f"\nmarket_summary: {ms}" if ms else ""))
    except Exception:
        parts.append("【分析预读缺块——按 AGENTS.md 的 DB_ACCESS 自查 analysis.db】")
    # ③ 账户参考（system_state 4 键）。
    # demo 分支（读 account_snapshots(profile='demo') 作资产/绩效展示）随
    # 2026-08-06 全量下线移除。
    try:
        con = _ro_db("account.db")
        rows = con.execute(
            "SELECT key, value, updated_utc FROM system_state WHERE key IN "
            "('live_totalEq','live_availBal','live_position_count',"
            "'last_live_account_check')").fetchall()
        con.close()
        if not rows:
            raise LookupError("no system_state keys")
        kv = "; ".join(f"{r['key']}={r['value']}(@{r['updated_utc']})" for r in rows)
        parts.append(f"【账户参考（system_state，仅参考——现仓/余额以 OKX API 为准）】\n  {kv}")
    except Exception:
        parts.append("【账户参考缺块——按 AGENTS.md 的 DB_ACCESS 自查 account.db】")
    # ③.5 Demo 合约可用性预载块随 2026-08-06 全量下线移除。它当年解决的是
    # 「analysis 按 live 行情选标的、Demo 池远小（实测 169 vs 400+）」导致 agent
    # 逐个试 API 烧预算的问题（2026-08-05T10:15 那轮烧光 720s 一条回执没写）。
    # ④ 决策简报（含历史正反样本与错失机会；每 cycle 一次）
    brief = _briefing_for_traders(cycle_id)
    if brief:
        parts.append("【决策简报（已预读，历史盈利/亏损/错失机会均为参考）】\n"
                     f"--- decision_briefing ---\n{brief}\n--- end ---")
    else:
        parts.append("【决策简报缺块——按 AGENTS.md 自跑 decision_briefing.py 兜底】")
    # ⑤ 必须自取项（防预载诱导偷懒）
    profile = "live"
    safe_cycle = cycle_id.replace(":", "-")
    facts_file = (f"<PROJECT_ROOT>/tmp/live_facts_{safe_cycle}.json").replace('<PROJECT_ROOT>', _public_project_path())
    # demo 的 role_policy（max-size 容量口径）随 2026-08-06 全量下线移除。
    role_policy = (
        ("Live OPEN/ADD 的组合保证金闸只认执行时同次 "
        "account.balance.imr/totalEq 与本单 incremental_order_imr，"
        f"预计成交后须≤{MAX_PORTFOLIO_IMR_RATIO:.1%}，超限整笔拒绝；"
        "mgnRatio/gross/net 不得替代，"
        "CLOSE/REDUCE 不受该闸影响。另有单笔增量保证金硬上限 "
        f"MAX_SINGLE_ORDER_IMR_RATIO={MAX_SINGLE_ORDER_IMR_RATIO:g}"
        f"（≤{MAX_SINGLE_ORDER_IMR_RATIO:.0%} 净值，定仓预算 "
        f"{MAX_SINGLE_ORDER_IMR_RATIO * SINGLE_ORDER_SIZING_HEADROOM_PCT:.1%} "
        "含滑点余量，"
        "2026-08-08 起）：validator 超限自动按 lotSz 缩量或整笔拒绝，提案前以 "
        "facts 的 balance.single_order_margin_budget_usdt 为准核对仓位，"
        "该预算作用域是下一笔 OPEN/ADD 增量，既有仓位不扣减它；组合总量另看 "
        "balance.portfolio_margin_state/portfolio_margin_label_cn 与 66.6% 闸，"
        "禁写‘既有仓 X% 接近单笔15%’、禁算 15%-X%、禁用 gross/net 判断保证金紧张，"
        "禁心算每张保证金、禁缩量后改参重试逼近上限。"
        "每个 OPEN/ADD 候选必须先运行 multitimeframe_decision_evidence.py，固定 "
        f"--cycle-id {cycle_id}，输出到 <PROJECT_ROOT>/tmp/mtf_{safe_cycle}_<symbol>.json；"
        "15m/1H/4H 必须全部 exact-ready，完整 evidence_contract 原样进入 "
        "decision_card.multitimeframe_analysis。OPEN/ADD 必须显式给出与 action 一致的 "
        "side=long/short；三个周期的 evidence 都必须是非空 JSON list[string]，"
        "分别给方向证据和唯一 rank 1/2/3，"
        "选择 rank=1；calibrated_confidence=null、confidence_claim_allowed=false。"
        "executor 会在账户/订单 I/O 前独立重读 market.db；当前三周期必须 ready。完全一致"
        "走 current_market_exact；同槽后续采集修订时，只接受 analysis_db_writer_validated"
        "锚点并保留 post_analysis_market_revision 与双时点 hash。禁止编辑、摘录或重算。"
        "任何 open_* 历史数字只认 find_similar_experience 以固定 cycle --as-of "
        "并直接传本卡 --entry/--stop/--target（禁止自行换算百分比或 RR）输出的 "
        "evidence_contract：原样写进 decision_card；只引用 exact_setup/"
        "same_symbol_similar/cross_symbol_similar 具名 summary，截断样例数组禁止计数或混栏。"
        "已取消的旧同侧/集中度硬规则不得恢复，回执不得复述其旧阈值，"
        "任何旧 MEMORY 或旧回执里的该规则无效；0.0666 也是错误阈值。").replace('<PROJECT_ROOT>', _public_project_path())
    )
    parts.append(
        ("【你仍需自取（唯一权威，禁用预载替代）】\n"
        "  一次性只读事实包：pwsh -NoProfile -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 "
        "<PROJECT_ROOT>/scripts/live_decision_facts.py "
        f"--profile {profile} --cycle-id {cycle_id} --out-file {facts_file}；"
        f"随后 read {facts_file}\n"
        "  该命令只能在同轮正式 analyst_writer 已返回 ok:true 后调用；脚本会在任何"
        "OKX读取和out-file写入前核对 analysis.db。同轮analysis尚未成功时返回"
        "analysis_authority_required_before_live_facts 且不生成facts；若误调用，立即回到"
        "analysis validate-only→正式writer，不得重试facts或把拒绝当analysis终态。"
        "  命令须原样直跑；该文件一次读取 OKX 现仓、余额、ctVal 和活动 SL，"
        "并确定性给出 position_age_hours、止损损失和 IMR 比例。禁止编辑该文件，"
        "禁止自行换算这些字段。live_decision_facts 工具必须先返回 ok:true，且必须先读取"
        f"已落盘的 {facts_file}，随后才可单独调用逐仓退出批处理：pwsh -NoProfile -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/"
        "multitimeframe_decision_evidence.py --db-root <PROJECT_ROOT>/db "
        f"--facts-file {facts_file} --cycle-id {cycle_id} --out-file "
        f"<PROJECT_ROOT>/tmp/position_exit_{safe_cycle}.json --decision-view-file "
        f"<PROJECT_ROOT>/tmp/position_exit_view_{safe_cycle}.json。facts与position_exit不得出现在同一条"
        "assistant响应或同一tool-call batch，禁止并行；批处理返回后只read具名"
        "position_exit_view精简文件；完整position_exit仅供runner验证，禁止加载。"
        "status=blocking 时禁止 OPEN/ADD；只有 "
        "action_policy.allowed_executor_actions 明确包含所选 "
        "close/reduce/adjust_protection 且原始现仓已核验，"
        "才保留去风险出口，否则不调用 executor，并写 terminal error 回执后停止。"
        "status=ok 时 exchange.positions/balance 才可用于新增风险判断。"
        "不论是否包含 OPEN/ADD，HOLD/WAIT、OPEN、ADD、CLOSE、REDUCE 与 "
        "ADJUST_PROTECTION 都只允许交给下述固定 runner；禁止临场拼 executor/"
        "writer Python 或按动作分支绕开 runner。OPEN/ADD 只在 plan 声明 "
        "target_stop_risk_pct_equity 与 lev，runner 读取本 cycle canonical analysis "
        "card、确定性定仓，并把逐笔 card 与完整 live_facts 绑定到回执。"
        "必须一次 write 完整 "
        f"<PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json，落盘后立即且只调用一次 "
        "live_position_action_runner.py；plan 后 30s 机器闸要求出现合法 runner marker；"
        "若完整 plan 已落盘 10 秒仍无合法 marker，stage supervisor 会确定性启动同一个 "
        "runner；你仍照常立即调用，profile 锁与 handoff CAS 保证只有一个执行。晚到调用若"
        "只返回并发锁或已有 started/executing/committed，不得重跑、改 plan 或宣称业务失败；"
        "actions=[] 即 HOLD，runner 不产生判断，只按你逐仓裁决执行、需要时先提交 "
        "runner_in_progress=true 的 interim superset，再同进程落唯一 final 回执。命令："
        "pwsh -NoProfile -File "
        "<PROJECT_ROOT>/scripts/run_okx_python.ps1 <PROJECT_ROOT>/scripts/live_position_action_runner.py "
        f"--cycle-id {cycle_id} --plan-file <PROJECT_ROOT>/tmp/position_plan_{safe_cycle}.json "
        f"--facts-file {facts_file} --receipt-file "
        f"<PROJECT_ROOT>/tmp/_receipt_live_{safe_cycle}.json --db-root <PROJECT_ROOT>/db。"
        "batch_status=partial|failed 或非零退出即 terminal failure，禁止重跑或写 HOLD 覆盖。"
        "facts 文件读完后禁止再读 trades_writer.py 源码、探查 schema、搜索历史回执或研究实现；"
        "立即决定并生成完整回执；writer 返回前禁止最终答复、禁止无内容 stop。writer 返回 ok:true 后"
        "严禁 query_db、--help、--schema 或任何其他"
        "工具，立即给出简短最终答复；stage_runner 独立核验本 cycle trade_cycles，Agent 不得重复"
        "核验。零成交 HOLD 也必须先落库且只能由 writer 完成。"
        f"{role_policy}").replace('<PROJECT_ROOT>', _public_project_path())
    )
    return "\n\n" + "\n\n".join(parts) + "\n"


def _write_message_file(key: str, msg: str) -> Path | None:
    """把触发消息写成 UTF-8 文件（--message-file 用），兼审计留痕。

      ① 使用官方 UTF-8 文件契约，消除编码、引号和长度对 argv 的依赖；
      ② 每轮触发指令落盘 logs/trigger/msg-<session-key>.txt，排障（改标/丢会话）
        可直接查当轮确切指令。写失败返 None，caller 回退 --message argv（保底不断链）。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        p = LOG_DIR / f"msg-{key}.txt"
        # newline="\n"：禁 Windows 文本模式把 \n 翻成 \r\n（否则 agent 收到的消息混入 CR，
        # 与源文差 60+ 字符——2026-07-03 金丝雀实测）。
        p.write_text(msg, encoding="utf-8", newline="\n")
        return p
    except OSError as e:
        print(f"[trigger_agent] WARN message-file 写失败，回退 --message argv: {e}",
              file=sys.stderr)
        return None


def build_cmd(stage: str, cycle_id: str, mode: str, db_root: str | Path | None = None) -> list[str]:
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = _resolve_db_root(db_root)
    agent = STAGE_AGENTS[stage]
    # 插入点 A0：tmp 遮蔽标准库探测（纯只读，只告警）。放在最前面：它与账本无关，
    # 失败也不该影响自愈/简报，而一旦命中就说明本轮任何成交都写不进去。
    _check_tmp_stdlib_shadow(stage, cycle_id)
    # 插入点 A：先自愈账本，再生成简报——顺序不可颠倒（简报要反映修好后的持仓）。
    # dry-run 走不到这里（fire 在 build_cmd 前就已返回），故干跑不会写库。
    autoheal = _autoheal_ledger(stage, cycle_id)
    if autoheal.get("blocking"):
        kinds = sorted({
            str(item.get("kind") or "UNKNOWN")
            for item in autoheal.get("findings", [])
            if isinstance(item, dict)
        })
        alert_state = (
            "alerted" if autoheal.get("alerted") is True
            else "alert_failed" if autoheal.get("p0") else "not_p0"
        )
        # **fail-safe：只告警不阻断**（主人 2026-08-05 拍板，事故后回退）。
        #
        # 曾经这里 raise 掉整个 stage。后果：demo 出现一个自愈范围内的幽灵仓 →
        # 每 2 分钟派发、每 2 分钟被挡、无人真正修 → demo 与依赖它的 **push 一起死锁
        # 2h14m**（2026-08-05 17:25→19:39）。症状（收不到推送）离根因（demo 账本幽灵仓）
        # 隔三层，且 60+ 次重试零告警。
        #
        # 为什么不该在派发层挡：真正的防线是 `order_executor` 的 pretrade 闸——它紧贴下单、
        # 用当场 API 现仓、只挡这一单。派发层阻断则连 push 一起杀，而 **push 是纯汇报、
        # 一分钱不碰**。对照：前一日 live 被 pretrade 闸冻结 9h 期间推送始终正常，
        # 系统保持可观测；本次派发层阻断直接让系统变哑。
        #
        # 所以：自愈尽力而为，修不成就让流程照常走，由 pretrade 闸 fail-closed 兜底。
        print(f"[trigger] WARN ledger_autoheal 未清干净但不阻断 "
              f"stage={stage} cycle={cycle_id} status={autoheal.get('status')} "
              f"rc={autoheal.get('rc')} findings={','.join(kinds) or 'UNKNOWN'} "
              f"alert={alert_state}（pretrade 闸仍会 fail-closed 兜底）",
              file=sys.stderr)
    # 触发消息只写"本轮工作"（stage/cycle/mode + analyst 的数据简报）；
    # 流程/红线/注意事项全在各 agent 的 AGENTS.md（OpenClaw 每轮自动加载），不再塞触发消息。
    if stage == "analyst":
        rollback_candidate_note = ""
        if thresholds.candidate_funnel_repair_active(cycle_id):
            candidate_paths = candidate_evidence_paths(cycle_id)
            brief = _analyst_briefing(
                cycle_id,
                candidate_out_file=candidate_paths["manifest"],
                ready_pool_out_file=candidate_paths["ready_pool"],
            )
            rollback_phase = thresholds.candidate_bundle_phase(cycle_id)
            rollback_state = prepare_candidate_bundle(
                cycle_id=cycle_id,
                db_root=resolved_db_root,
                phase=(rollback_phase if rollback_phase in {"shadow", "consume"}
                       else "shadow"),
                timeout_seconds=thresholds.candidate_bundle_timeout_seconds(
                    cycle_id),
            )
            rollback_candidate_note = (
                "【人工analysis回滚最小合同】policy=no_three_period_no_six_card_v1；"
                "manifest side-neutral，最终side由轻量OPEN signal选择；禁止三周期、四态、"
                "MTF、history、EV与六项卡。"
                if thresholds.minimal_decision_contract_active(cycle_id) else
                "【人工analysis回滚轻量OPEN】candidate_id/MTF/history/EV不再是OPEN条件；"
                "使用全量manifest与有界decision slice，OPEN写轻量价格计划，"
                "四态候选不OPEN时必须给可复算primary_disqualifier。"
                if thresholds.decision_restriction_removal_active(cycle_id) else
                "【人工analysis回滚候选身份】"
                f"manifest={candidate_paths['manifest']}、"
                f"bundle={candidate_paths['bundle']}、"
                f"status={rollback_state.get('status')}。"
                "每个OPEN先以manifest内candidate_id运行多周期工具，signal顶层与"
                "raw.candidates_deep_dived_v2均原样携带同一candidate_id；"
                "禁止自由--symbol别名。manifest无效时本轮OPEN失败关闭。"
            )
        else:
            brief = _analyst_briefing(cycle_id)
        brief_block = (
            "\n【本轮数据简报（已预读，直接据此分析）】\n"
            f"--- decision_briefing ---\n{brief}\n--- end ---\n"
        ) if brief else "\n"
        msg = (
            f"OKX 本轮工作：stage=analyst cycle={cycle_id} mode={mode}。"
            f"本轮 gate/落库/回执的 cycle_id 一律用上面的 cycle={cycle_id}，"
            f"即使你的会话晚起、墙钟已进下一槽也不换标（禁 cycle_id_for() 重解析）。"
            f"{rollback_candidate_note}"
            f"按你的 AGENTS.md（操作手册）执行本轮分析。{brief_block}"
        )
    elif stage == "live" and mode == "unified":
        # 统一 live 在 analysis 尚未产出时起棒：预载采集后的全量 briefing，
        # 同一会话先 analyst_writer，成功后再走 OKX API + executor + trades_writer。
        candidate_phase = thresholds.candidate_bundle_phase(cycle_id)
        funnel_active = thresholds.candidate_funnel_repair_active(cycle_id)
        candidate_state = {
            "phase": candidate_phase,
            "status": "OFF",
            "fallback_required": False,
        }
        if candidate_phase in {"shadow", "consume"} or funnel_active:
            candidate_paths = candidate_evidence_paths(cycle_id)
            brief = _analyst_briefing(
                cycle_id,
                candidate_out_file=candidate_paths["manifest"],
                ready_pool_out_file=candidate_paths["ready_pool"],
            )
            if candidate_phase in {"shadow", "consume"}:
                # Batch rollout remains independent from the always-on funnel
                # artifacts and exact identity contract.
                candidate_state = prepare_candidate_bundle(
                    cycle_id=cycle_id,
                    db_root=resolved_db_root,
                    phase=candidate_phase,
                    timeout_seconds=thresholds.candidate_bundle_timeout_seconds(
                        cycle_id),
                )
            else:
                try:
                    manifest, _ = load_candidate_manifest(
                        candidate_paths["manifest"], cycle_id)
                    ready_pool = (
                        manifest.get("ready_pool")
                        if isinstance(manifest.get("ready_pool"), dict) else {})
                    candidate_state = {
                        "phase": candidate_phase,
                        "status": "MANIFEST_ONLY",
                        "manifest_valid": True,
                        "manifest_path": str(candidate_paths["manifest"]),
                        "ready_pool_path": str(candidate_paths["ready_pool"]),
                        "briefing_sha256": manifest.get("manifest_sha256"),
                        "candidate_count": manifest.get("candidate_count"),
                        "full_ready_count": ready_pool.get("ready_count"),
                        "ready_pool_status": str(
                            ready_pool.get("status") or "PASSED").upper(),
                        "ready_pool_sha256": ready_pool.get("sha256"),
                        "decision_consumes_bundle": False,
                        "fallback_required": False,
                        "production_database_writes": 0,
                        "orders_placed": 0,
                    }
                except Exception as exc:  # noqa: BLE001 - OPEN identity fails closed
                    candidate_state = {
                        "phase": candidate_phase,
                        "status": "DEGRADED",
                        "manifest_valid": False,
                        "manifest_path": str(candidate_paths["manifest"]),
                        "ready_pool_path": str(candidate_paths["ready_pool"]),
                        "error": f"{type(exc).__name__}:{exc}",
                        "decision_consumes_bundle": False,
                        "fallback_required": True,
                        "production_database_writes": 0,
                        "orders_placed": 0,
                    }
        else:
            brief = _analyst_briefing(cycle_id)
        # Message construction happens after the batch so the dynamic upper
        # bound is recomputed from the true remaining Gateway/finalize budget.
        msg = _unified_live_message(
            cycle_id, brief, candidate_bundle=candidate_state)
    elif stage == "live":
        # trader 预载减少冷启动逐库自查；各块独立 fail-safe，缺块留标记回退自查。
        preload = _trader_preload(cycle_id, stage)
        msg = (
            f"OKX 本轮工作：stage={stage} cycle={cycle_id} mode={mode}。"
            f"本轮写库/回执/executor 调用的 cycle_id 一律用上面的 cycle={cycle_id}，"
            f"即使会话晚起、墙钟已进下一槽也不换标（禁墙钟重解析）。"
            f"按你的 AGENTS.md（操作手册）执行。{preload}"
        )
    else:
        msg = (
            f"OKX 本轮工作：stage={stage} cycle={cycle_id} mode={mode}。"
            f"按你的 AGENTS.md（操作手册）执行。"
        )
    key = session_key(stage, cycle_id, resolved_db_root)
    msg_file = _write_message_file(key, msg)
    msg_args = (["--message-file", str(msg_file)] if msg_file
                else ["--message", msg])
    timeout = (_unified_live_timeout_seconds(cycle_id)
               if stage == "live" and mode == "unified"
               else STAGE_TIMEOUTS.get(stage, 720))
    return _agent_launcher() + [
        "agent",
        "--agent", agent,
        "--session-key", key,
        *msg_args,
        "--timeout", str(timeout),
        "--json",
    ]


def _fire_push_script(cycle_id: str, mode: str = "full", db_root: str | Path | None = None) -> str:
    """push stage 纯脚本路径：detached 起 push_pipeline.py
    （build→render→validate→qq_push→archive→system_state）。
    返回 session-key 作 card_id（与 agent 路径同签名，dispatcher._fire_stage 语义不变）。
    dry-run（OKX_TRIGGER_DRYRUN=1）只落命令日志不真起。python.exe 直起（原生 exe，
    DETACHED 存活；不经 pwsh wrapper——pwsh 跑 .ps1 在 DETACHED_PROCESS 下可能静默不执行；
    管道内部各步自走 wrapper 拿 UTF-8/MX_APIKEY）。
    起棒失败抛异常由 _fire_stage 释放闩锁重试。"""
    cycle_id = validate_cycle_id(cycle_id)
    resolved_db_root = _resolve_db_root(db_root)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key("push", cycle_id, resolved_db_root)
    inner_cmd = [_PYTHON_EXE, _PUSH_PIPELINE, "--cycle", cycle_id,
                 "--db-root", str(resolved_db_root)]
    if mode == "failure_report":
        # push_pipeline 会独立重验未来激活边界、精确 cycle 身份、failed 终态
        # 与 profile 租约释放；这里只传递意图，不携带可伪造的失败详情。
        inner_cmd.append("--upstream-failure-report")
    elif mode == "degraded_report":
        # P0-5 5b：同样只传意图。push_pipeline 发送前独立复核 report barrier
        # 是否仍未就绪；已就绪则自动升级回完整业务报告并走完整终态硬闸，
        # 故此 flag 无法用来把一份正常战报降级、绕开业务终态凭证。
        inner_cmd.append("--degraded-report")
    cmd = _supervised_cmd("push", cycle_id, mode, inner_cmd, db_root=resolved_db_root)
    logf = LOG_DIR / f"{key}.log"
    dry = os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
    with open(logf, "a", encoding="utf-8") as fh:
        fh.write(
            f"\n[{now_cst()}] stage=push cycle={cycle_id} mode={mode} "
            f"execution=script dry={dry}\n"
        )
        fh.write("  cmd: " + " ".join(cmd) + "\n")
        if dry:
            fh.write("  (dry-run: 未真起 push_pipeline)\n")
            return key
        fh.flush()
        # 仅 DETACHED（无控制台，stdout 重定向到 log）——同 fire()，Windows 下不弹窗。
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=flags, cwd=str(Path(_public_project_path())), close_fds=True)
        _probe_launch(proc, "push", cycle_id, fh, resolved_db_root)
    return key


def fire(
    stage: str,
    cycle_id: str,
    mode: str = "full",
    db_root: str | os.PathLike | None = None,
) -> str:
    """detached 拉起 Agent stage，或对 push 无条件起纯脚本管道。

    返回 session-key（作 card_id 用）。

    dry-run（OKX_TRIGGER_DRYRUN=1）：不真起，只把命令落日志——tmp 验证 plumbing 用。
    启动失败（如 openclaw 不在 PATH）会抛 FileNotFoundError，由 dispatcher._fire_stage
    捕获后释放 stage 闩锁，下一 tick 重试。
    """
    cycle_id = validate_cycle_id(cycle_id)
    if stage == "push":
        if mode in {"full", "script"}:
            return _fire_push_script(cycle_id, db_root=db_root)
        return _fire_push_script(cycle_id, mode, db_root=db_root)
    if stage not in STAGE_AGENTS:
        raise ValueError(f"unknown stage: {stage}")
    resolved_db_root = _resolve_db_root(db_root)
    dry = os.environ.get("OKX_TRIGGER_DRYRUN") == "1"
    if not dry and os.path.normcase(os.fspath(resolved_db_root)) != os.path.normcase(
        os.fspath(_CANONICAL_DB_ROOT)
    ):
        raise RuntimeError(
            "non-default db_root is supported for Agent stages only with "
            "OKX_TRIGGER_DRYRUN=1; Gateway tool DB-root propagation is not guaranteed"
        )
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    key = session_key(stage, cycle_id, resolved_db_root)
    logf = LOG_DIR / f"{key}.log"

    if dry:
        # dry 判定必须在 build_cmd 之前，避免启动 decision_briefing 子进程或写
        # msg/briefing 文件。dry 只验 plumbing：落意图日志即返回，
        # 不组消息、不起任何子进程、不写消息/简报文件。
        with open(logf, "a", encoding="utf-8") as fh:
            fh.write(f"\n[{now_cst()}] stage={stage} cycle={cycle_id} mode={mode} "
                     f"agent={STAGE_AGENTS[stage]} dry=True\n")
            fh.write("  (dry-run: 未组消息/未真起 agent)\n")
        return key
    inner_cmd = build_cmd(stage, cycle_id, mode, db_root=resolved_db_root)
    cmd = _supervised_cmd(
        stage, cycle_id, mode, inner_cmd, db_root=resolved_db_root
    )
    with open(logf, "a", encoding="utf-8") as fh:
        fh.write(f"\n[{now_cst()}] stage={stage} cycle={cycle_id} mode={mode} "
                 f"agent={STAGE_AGENTS[stage]} dry={dry}\n")
        fh.write("  cmd: " + " ".join(cmd) + "\n")
        fh.flush()
        # 仅 DETACHED（无控制台）：DETACHED 与 CREATE_NO_WINDOW 互斥(MSDN)，同设致隐藏失效
        # → 子进程获控制台 → Windows Terminal DefTerm 弹「openclaw-agent」窗。
        # stdout/stderr 已重定向到 log（见上），DETACHED 单独安全、无控制台。
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            cwd=_public_project_path(),
            close_fds=True,
        )
        _probe_launch(proc, stage, cycle_id, fh, resolved_db_root)
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description="agent 起棒适配层（唯一 caller=core/dispatcher.py；人工排障可 CLI 调）")
    ap.add_argument("--stage", required=True, choices=sorted((*STAGE_AGENTS, "push")))
    ap.add_argument("--cycle", required=True, help="cycle_id 如 2026-06-18T14:00")
    ap.add_argument("--mode", default="full", choices=["full", "unified"])
    args = ap.parse_args()
    if args.mode == "unified" and args.stage != "live":
        ap.error("mode=unified 仅适用于 stage=live")
    key = fire(args.stage, args.cycle, args.mode)
    print(f"fired stage={args.stage} cycle={args.cycle} -> session-key={key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
