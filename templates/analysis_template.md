<!--
doc: analysis_template
doc-version: V2.3-template
last-updated: 2026-09-02
updated-by: Codex
change-summary: closure周期机会优先+轮换、全manifest可OPEN，清除identity/MTF与软观察单独否决。
-->

# 分析回执模板

`minimal_contract_full_closure_v1` 周期的manifest保留全市场symbol；review slice只决定
本轮深入判断预算，不能限制manifest内可形成OPEN的symbol。单候选id或单symbol命令不提供
准入授权。closure review按机会分优先并保留少量轮换；成交额/OI只参与连续排序。

前向策略：`no_three_period_no_six_card_v1`，继承上代
`all_market_lightweight_open_v1` 的 `lightweight_open_v1` 机器执行包。历史
`decision_card_v1` 与MTF工件只读兼容，不得用于新cycle。

```json
{
  "cycle_id":"2026-09-02T12:00",
  "ts":"2026-09-02 12:05:00",
  "mode":"full",
  "status":"ok",
  "decision_protocol":"minimal_decision_v2",
  "regime":"range",
  "regime_stale":0,
  "market_summary":{"macro":{},"news":{"events":[]},"tech":{},"sentiment":{},"quant":{}},
  "missing_sources":[],
  "signals":[{
    "symbol":"SOL-USDT-SWAP","action":"open_long","side":"long",
    "reasoning":"Agent选择long","entry_hint":100,"stop_hint":97,
    "tp_hint":106,"exit_mode":"fixed_tp"
  }],
  "raw":{
    "candidates_deep_dived_v2":[{
      "symbol":"SOL-USDT-SWAP","decision":"provisional_open",
      "reason":"本轮选择OPEN"
    }],
    "candidate_coverage":{"dynamic_limit":1,"stop_reason":"target_reached"}
  }
}
```

规则：

- manifest/slice每symbol一行，side由Agent在signal中选择。
- 不写15m/1H/4H判断、四态、mature/early、MTF/history/EV/六项卡。
- reject项只写reason；OPEN项必须有同symbol唯一OPEN signal。
- 含MTF/三周期授权的reason拒写；成交额/OI、无催化、已有仓位数或未触顶IMR不得单独reject。
- entry/stop/target/exit_mode是SL/TP机器执行输入，仍须正有限且方向几何正确。
- 强制校验五段market_summary、news.events、side-neutral review覆盖及轻量OPEN几何。
- UTF-8整文件先validate-only，再以完全相同字节正式writer；禁止手写SQL。
