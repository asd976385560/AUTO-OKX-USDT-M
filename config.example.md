<!--
doc-version: V2.0-public-example
last-updated: 2026-08-04
updated-by: Codex
change-summary: Add separate alert routing and explicit ledger-autoheal write opt-ins while keeping all defaults inert.
-->

# V2.0 本地配置示例

本文件只包含占位符。复制为 `config.md` 后填写本机值：

```powershell
Copy-Item config.example.md config.md
```

`config.md` 已被 `.gitignore` 排除。不要把真实 API Key、Secret、Passphrase、Token、Webhook、QQ 目标、账户标识或私网地址提交到 Git。

运行时优先读取环境变量；下列表格只为现有采集器提供受控 fallback。

## 1. 项目与运行环境

| 配置 | 示例占位符 |
|---|---|
| `OKX_ROOT` | `<PROJECT_ROOT>` |
| `OKX_DB_ROOT` | `<PROJECT_ROOT>/db` |
| `OKX_PYTHON_BIN` | `<PATH_TO_PYTHON>` |
| `OKX_SITE_PACKAGES` | `<OPTIONAL_SITE_PACKAGES_PATH>` |
| `OKX_OPENCLAW_STATE_DB` | `<USER_HOME>/.openclaw/state/openclaw.sqlite` |
| `OKX_PROXY_URL` | `<OPTIONAL_PROXY_URL>` |
| `MX_DATA_PATH` | `<OPTIONAL_PATH_TO_MX_DATA_PY>` |

`OKX_OPENCLAW_STATE_DB` is the public deployment name. The legacy
`OPENCLAW_STATE_DB` alias remains readable for compatibility, but the prefixed
name takes precedence when both are set.

## 2. OKX 凭证

OKX API Key、Secret 和 Passphrase 由仓库外的 OKX CLI profile 或部署环境管理。本仓库不解析本节中的真实值，也不提供默认凭证。

| 配置 | 占位符 |
|---|---|
| API Key | `<OKX_API_KEY>` |
| Secret | `<OKX_API_SECRET>` |
| Passphrase | `<OKX_API_PASSPHRASE>` |

## 4. 外部数据源

### 4.1 FRED

首选环境变量：`FRED_API_KEY` 或 `FRED_KEY`。

| 配置 | 值 |
|---|---|
| API Key | `<FRED_API_KEY>` |

### 4.3 CoinGecko

首选环境变量：`COINGECKO_API_KEY`、`CG_API_KEY` 或 `COINGECKO_KEY`。

| 配置 | 值 |
|---|---|
| API Key | `<COINGECKO_API_KEY>` |

### 4.4 妙想资讯

首选环境变量：`MX_APIKEY`。

| 配置 | 值 |
|---|---|
| API Key | `<MX_APIKEY>` |

## 5. 推送与外部集成

公开代码不带推送目标。只有在明确需要外发时才设置：

| 环境变量 | 占位符 |
|---|---|
| `OKX_QQ_TARGET` | `group:<QQ_GROUP_OPENID>` |
| `OKX_QQ_ALERT_TARGET` | `c2c:<QQ_USER_OPENID>` |
| `OKX_OPENCLAW_MJS` | `<PATH_TO_OPENCLAW_MJS>` |
| `OKX_NODE_BIN` | `<PATH_TO_NODE>` |
| `OKX_CLI_ENTRY` | `<PATH_TO_OKX_CLI_ENTRY>` |

验证和开发环境建议始终设置：

```powershell
$env:OKX_EXECUTOR_DRYRUN = '1'
$env:OKX_TRIGGER_DRYRUN = '1'
```

非默认 `OKX_DB_ROOT` 只用于确定性脚本与上述 dry-run。公开触发器会拒绝在非默认
root 上真实启动 Gateway Agent，避免远端工具进程回落到 `<PROJECT_ROOT>/db`。

Live 账本 autoheal 永久只读；下列开关只允许 Demo 写入。只有在隔离 Demo 数据库完成
dry-run 和备份验证后，才分别启用：

```powershell
$env:OKX_LEDGER_AUTOHEAL_APPLY = '1'          # 仅 Demo：允许精确 GHOST close 补账
$env:OKX_LEDGER_AUTOHEAL_UNRECORDED = '1'     # 仅 Demo：允许 intent+ordId 一致且已确认同侧足量止损的精确 UNRECORDED open 补账
```

第二个开关单独设置不生效。Live 修复只能走 unique ordId、写前已验证备份、逐笔 apply
和写后现仓/reconciliation/invariants 复核的人工流程。自愈只写账本，不下单或重放订单。
