<!--
doc: push_template
doc-version: V2.4-template
last-updated: 2026-09-06
updated-by: Codex
change-summary: 21:30起标题按本轮开始到业务完成计时，缺失证据显示未知；保留现有交易报告合同。
-->

# 推送模板 — format=3

```text
【HH:MM】第N轮 / ⏱Xs / live / 动作 币种
Agent自主裁决 | 摘要

📊 资产
🟢 实盘：资金 $X | 可用USDT $X | 累计收益(交易PnL·未扣费) X USDT | N仓

💼 持仓详情
<每仓真实mark/PnL/SL>

🛡 风控
Live组合保证金 X% | 上限66.6% | 杠杆 Xx/10x | PASS

🌍 行情
BTC $X | ETH $X | 24h回归预报=X | USD_BROAD X

🎯 Agent裁决
<reasoning与逐仓结论>

⚙️ 执行
<成交、拒绝、账实指纹与保护回读>

⏰ 时间线
下次HH:00: Xmin | 下次复盘: 08:05
耗时口径：本轮开始→业务完成

⚠️ 异常
无
```

自北京时间 `2026-09-06T21:30` 起，标题 `⏱Xs` 取同轮槽位起点到已核验业务完成时间的秒数。
起止时间统一按带时区的北京时间解析；读取同轮 live 成功回执、采集完成凭证、analysis 成功行，
并与 `trade_cycles.raw.business_terminal` 核对，不使用派发时刻、渲染时刻或 Agent 自报耗时。
缺失、失败、时间非法或轮次不匹配时显示 `⏱未知`，异常段说明原因；不得以 0 替代。
校验器只读复算数字耗时；明确未知允许报告继续。此处不改变业务成功或 SLA 判定。
历史归档按原版本保留，不重写或重推。

前向 `minimal_decision_v2` 报告禁止出现 `🧩 三周期判断`、`🧭 六项决策卡`。
OPEN成交上下文只允许扁平 `open_execution_package_v1`，不得回显decision_card或包内额外证据。
历史归档不重写。账实成交数、业务指纹、报告间成交区间、降级横幅、换行和归档前硬检继续保留。

format=3 当前强制校验16 项、9 个 emoji 锚点。流水线顺序固定：
render/validate -> `push_archive` -> `qq_push`，归档硬检必须先于发送。

- 执行审计前向边界：`2026-08-14T02:15`；历史归档不反向加责、不补推。
- 持仓投影合同：`positions_projected_cycle`，成交区间为 `(facts.as_of, 构建时点]`。
- 纯保护动作标记 `protection_only=true`；无交易所副作用标记
  `exchange_side_effect=none`。

当前决策、业务及执行证据仍受版本化硬校验；历史 MTF 要求仅适用于各自激活窗口。
