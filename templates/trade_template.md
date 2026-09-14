<!--
doc: trade_template
doc-version: V2.3-template
last-updated: 2026-09-02
updated-by: Codex
change-summary: closure使用stage facts交接与精确扁平open_execution_package_v1，禁止旧decision_card键。
-->

# 交易plan与回执模板

`minimal_contract_full_closure_v1` 周期先等待具名
`live_input_handoff_<safe_cycle>.json` 为 `ready`，再读取其中绑定的facts和逐仓view。
Agent不得自行运行私有facts脚本；handoff失败、cycle/hash/count不一致时不得生成plan。

runner在可信读取边界把旧analysis列转换为以下唯一执行包；plan不得手写它：
历史 `all_market_lightweight_open_v1` / `lightweight_open_v1` 仅作为旧列读取兼容，
不得原样进入closure回执或账本raw。

```json
{"contract":"open_execution_package_v1","entry":100,"stop":97,"target":106,"exit_mode":"fixed_tp"}
```

执行包禁止side、reasoning、risk_reward、news_context、regime_scope及任何额外字段。

```json
{
  "cycle_id":"2026-09-02T12:00",
  "receipt_context":{
    "cycle_id":"2026-09-02T12:00","mode":"live","status":"ok",
    "regime":"range","decision_protocol":"minimal_decision_v2",
    "reasoning":"本轮总体判断",
    "position_reviews":[
      {"symbol":"BTC-USDT-SWAP","side":"long","decision":"HOLD","reason":"未触发退出"}
    ]
  },
  "actions":[
    {"action":"OPEN","symbol":"SOL-USDT-SWAP","side":"long",
     "target_stop_risk_pct_equity":0.0075,"lev":5}
  ]
}
```

- plan与HOLD/退出回执不含decision_card或六项字段。
- OPEN/ADD由runner从analysis_signals兼容列提取 `open_execution_package_v1`；
  canonical OPEN 缺失即拒绝。executor不再要求三周期、历史经验或六项展示卡。
- CLOSE/REDUCE/ADJUST_PROTECTION只使用动作参数和reasoning。
- SL、10x、15%、5%、98%、1%、66.6%以及账户/账仓/intent、成交与保护回读保持。
- runner在同一确定性 Python 进程内执行成交并调用
  `commit_receipt(receipt, "live")`；失败/uncertain不得用HOLD覆盖，不得伪造OPEN fill。
