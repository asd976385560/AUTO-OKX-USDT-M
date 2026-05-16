# 定时任务配置方法

## 当前配置（OpenClaw Cron）

| 任务 | 名称 | Cron 表达式 | 触发偏移 |
|------|------|------------|---------|
| Job A | OKX-JobA-快速采集 | `1,16,31,46 * * * *` | :01 :16 :31 :46 |
| Job E | OKX-JobE-慢源采集 | `10 * * * *` | :10 每小时 |
| Job B | OKX-JobB-决策执行 | `5,20,35,50 * * * *` | :05 :20 :35 :50 |
| Job C | OKX-JobC-复盘 | `30 0 * * *` | 每日 00:30 |

## 错开设计原理

```
:01  Job A 开始采集（约 30-60 秒完成）
:05  Job B 开始决策（此时 A 数据已写入）
:10  Job E 开始慢源采集（与 A/B 完全错开）
:16  Job A 下一轮采集
:20  Job B 下一轮决策
...
```

## OpenClaw Cron 配置示例

### Job A

```json
{
  "name": "OKX-JobA-快速采集",
  "schedule": { "kind": "cron", "expr": "1,16,31,46 * * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "运行 OKX Job A 快速采集: py E:\\OKX\\scripts\\collect_data.py --profile live --db-root E:\\OKX\\db",
    "model": "MiniMax-M2.7-highspeed",
    "thinking": "off",
    "lightContext": true,
    "timeoutSeconds": 666
  },
  "delivery": { "mode": "none" }
}
```

### Job E

```json
{
  "name": "OKX-JobE-慢源采集",
  "schedule": { "kind": "cron", "expr": "10 * * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "运行 OKX Job E 慢源采集: py E:\\OKX\\scripts\\collect_slow.py --db-root E:\\OKX\\db",
    "model": "MiniMax-M2.7-highspeed",
    "thinking": "off",
    "lightContext": true,
    "timeoutSeconds": 999
  },
  "delivery": { "mode": "none" }
}
```

### Job B

```json
{
  "name": "OKX-JobB-决策执行",
  "schedule": { "kind": "cron", "expr": "5,20,35,50 * * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "<见 prompts/jobb-prompt.md>",
    "timeoutSeconds": 888
  },
  "delivery": {
    "mode": "announce",
    "channel": "qqbot",
    "to": "<QQ Bot 目标 ID>"
  }
}
```

### Job C

```json
{
  "name": "OKX-JobC-复盘",
  "schedule": { "kind": "cron", "expr": "30 0 * * *" },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "<见 prompts/jobc-prompt.md>",
    "timeoutSeconds": 999
  },
  "delivery": {
    "mode": "announce",
    "channel": "qqbot",
    "to": "<QQ Bot 目标 ID>"
  }
}
```

## 注意事项

- Job A/E 不需要推送（`delivery.mode: "none"`）
- Job B/C 推送到 QQ Bot（`delivery.mode: "announce"`）
- Job B timeout 888s（< 900s = 15 分钟间隔）
- Job B 使用默认模型（非 MiniMax），因为决策需要更强的推理能力
- Job A/E 使用 MiniMax-M2.7-highspeed 且 `thinking=off`（采集任务，提示词尽量短，快速+低成本）
- Job E 默认 1H/4H 每小时采集，1D 每 4 小时采集，1W/1M 每日 UTC 0 点采集；需要全周期刷新时在 message 中追加 `--force-all-timeframes`
