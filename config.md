# config.md — OKX v3.0 交易系统运行时配置

> 本文件是**小灵交易系统的运行时配置中心**。
> 所有路径、API 密钥、数据库位置、定时任务频率均在此集中声明。
>
> ⚠️ **首次使用前，必须先填写下方必填项，然后执行初始化脚本。**

---

## 0. 首次初始化（必读）

### 必填项清单

| # | 必填项 | 在本文件位置 | 说明 |
|---|--------|------------|------|
| 1 | 数据库目录 | §1 | SQLite 数据库存放路径 |
| 2 | FRED API Key | §4.1 | 美联储经济数据接口 |
| 3 | CoinGecko API Key | §4.3 | 加密市场宏观数据接口 |
| 4 | 妙想 API Key | §4.4 | 东方财富资讯接口 |
| 5 | QQ Bot 推送目标 | §8 | QQ 推送通道 ID |

### 初始化命令

```bash
python scripts/init_okx2.py --root E:\OKX --db-dir <数据库目录>
```

---

## 1. 系统目录

| 项目 | 路径 | 说明 |
|------|------|------|
| 项目根目录 | `E:\OKX` | skill.md / config.md / scripts / docs 所在目录 |
| 数据库目录 | `E:\OKX\db` | SQLite 数据库文件存放目录（可配置） |
| 临时目录 | `E:\OKX\tmp` | 临时目录（临时测试、调试脚本，项目临时文件存放地方，临时文件只允许存放在这里，不允许存放在别的目录） |

初始化后自动生成的目录结构：

| 目录 | 说明 |
|------|------|
| `scripts/` | Python 采集/决策/复盘脚本（8 个核心脚本） |
| `prompts/` | Cron Agent Prompt 备份 |
| `reports/jobb/` | JobB 每轮决策报告 |
| `reports/self-reviews/` | JobC 每日自省报告 |
| `reports/daily/` | 日报备份 |
| `docs/` | 项目文档 |

其他固定路径：

| 项目 | 说明 |
|------|------|
| OKX CLI 配置 | `~/.okx/config.toml`（OKX API 凭证，不在项目目录） |

---

## 2. 数据库配置

| 数据库 | 用途 | 状态 |
|--------|------|------|
| market.db | 行情 / K线 / 跨市场 / 衍生品 | 初始化后可用 |
| news.db | 新闻 / 事件 / 币种情绪 | 初始化后可用 |
| account.db | 账户 / 持仓 / 评分 / 事件 / 报告 / 系统状态 / 经验库 | 初始化后可用 |
| lessons.db | 自省 / 学习 / 信号表现 / 错判模式 | 初始化后可用 |

### 数据库编码与时间

| 项目 | 值 |
|------|-----|
| 编码 | UTF-8 |
| 时间格式 | ISO8601 UTC（存储） |
| 显示时区 | UTC+8（CST） |
| 去重策略 | `(ts, symbol[, tf])` 为 PRIMARY KEY + `INSERT OR REPLACE` |
| 存储模式 | 增量追加（时间序列），非全量覆盖 |

---

## 3. OKX API 配置

| 项目 | 值 |
|------|-----|
| CLI 工具 | `okx`（npm 全局安装 `@okx_ai/okx-trade-cli`） |
| Profile | `live`（实盘，写死，禁止 `demo`） |
| 认证方式 | `~/.okx/config.toml` |
| API Base URL | `https://www.okx.com` |

### OKX 接口限速参考

| 端点类型 | 限速 |
|----------|------|
| 公共市场数据 | 20 req / 2s per IP |
| 账户/交易 | 10 req / 2s per UID |
| 下单 | 60 req / 2s per UID |

---

## 4. 外部数据源配置

### 4.1 FRED（美联储经济数据）

| 项目 | 值 |
|------|-----|
| Base URL | `https://api.stlouisfed.org/fred` |
| API Key | <REDACTED_FRED_API_KEY> |
| 采集指标 | DXY（`DTWEXBGS`）、VIX（`VIXCLS`）、S&P500（`SP500`） |
| 输出字段 | 当前值 + 日变化率 |
| 免费限额 | 120 req / min |

### 4.2 DefiLlama（全链 TVL）

| 项目 | 值 |
|------|-----|
| 历史TVL | `https://api.llama.fi/v2/historicalChainTvl` |
| 当前TVL | `https://api.llama.fi/v2/chains` |
| API Key | 无需（公开接口） |
| 采集频率 | 每 1 小时 |

### 4.3 CoinGecko（加密市场宏观数据）

| 项目 | 值 |
|------|-----|
| Global 数据 | `https://api.coingecko.com/api/v3/global` |
| API Key | <REDACTED_COINGECKO_API_KEY> |
| 采集指标 | BTC 市占率 + 加密总市值 + 24h 总交易量 |
| 免费限额 | 30 req / min（Free tier） |

### 4.4 妙想资讯（东方财富）

| 项目 | 值 |
|------|-----|
| 接入方式 | 通过 `mx-search` 技能 |
| API Key | <REDACTED_MX_API_KEY> |
| 地缘关键词 | 中东 / 中美 / 俄乌 / 朝鲜 / 宏观风险 / 美国总统 / 贸易战 |
| A 级标记 | 战争 / 冲突 / 核 / 制裁 / 军事 |

---

## 5. 定时任务配置

| 任务 | 频率 | Cron 表达式 | 触发方式 | 脚本/Agent |
|------|------|------------|---------|-----------|
| Job A — 快速采集 | 每 15 分钟 | `1,16,31,46 * * * *` | isolated agentTurn | `python scripts/collect_data.py --profile live` |
| Job E — 慢源采集 | 每 1 小时 | `10 * * * *` | isolated agentTurn | `python scripts/collect_slow.py --db-root <DB>` |
| Job B — 决策执行 | 每 15 分钟 | `5,20,35,50 * * * *` | isolated agentTurn | 完整 Agent prompt（见 prompts/jobb-prompt.md） |
| Job C — 自我复盘 | 每日 1 次 | `30 0 * * *` | isolated agentTurn | 完整 Agent prompt（见 prompts/jobc-prompt.md） |

> 错开设计：A 在 x1/x6 采集 → B 在 x5/x0 决策（给 A 4 分钟完成写入）→ E 在 x0 慢源采集

---

## 6. 交易品种

| 项目 | 值 |
|------|-----|
| 市场类型 | OKX USDT-M 永续合约（Swap） |
| 品种范围 | **无白名单限制**，全部 USDT-M 永续合约均可交易 |
| 品种发现 | `okx market instruments --instType SWAP` 动态获取 |
| 全币种扫描 | Job B 每轮扫描 300+ 币种，ctVal 过滤后五维评分 |

---

## 7. 风险硬上限配置

| 限制项 | 默认推荐档 | 契约硬上限 |
|--------|-----------|-----------|
| 单笔仓位占净值 | ≤ **5%** | ≤ **10%** |
| 杠杆（BTC/ETH） | **10x** | ≤ **10x** |
| 杠杆（山寨） | **5x** | ≤ **10x** |
| 同侧暴露 | ≤ 50% | ≤ **60%** |
| 并发持仓 | ≤ 3 仓 | ≤ **6 仓** |
| 单次开仓保证金上限 | — | ≤ **10% 净值** |

---

## 8. QQ Bot 推送配置

| 项目 | 值 |
|------|-----|
| 推送通道 | QQ C2C |
| 目标 ID | <REDACTED_QQ_C2C_TARGET_ID> |
| 推送内容 | 每轮决策要点（简洁报告） |
| 推送失败处理 | 推送失败 ≠ 交易失败；事件已落盘即成功 |

---

## 9. OKX CLI 技能路由

| 需求 | 技能 | 安装 |
|------|------|------|
| 行情数据 / K线 / 深度 / 资金费率 / OI | `okx-cex-market` | OpenClaw 技能 |
| 账户余额 / 持仓 / P&L / 转账 | `okx-cex-portfolio` | OpenClaw 技能 |
| 下单 / 撤单 / 改单 / 止损止盈 | `okx-cex-trade` | OpenClaw 技能 |
| Grid 网格 / DCA 马丁 | `okx-cex-bot` | OpenClaw 技能 |
| 地缘新闻搜索 | `mx-search` | OpenClaw 技能 |

---

## 10. 运行环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10+ |
| Python | 3.14+（需 `sqlite3` 标准库） |
| PowerShell | pwsh 7+（禁止使用 PS 5.1） |
| Node.js | 18+（OKX CLI 依赖） |
| OKX CLI | `npm install -g @okx_ai/okx-trade-cli` |
| OpenClaw | 最新版（Cron 定时任务依赖） |
| SQLite | 3.x（Python 自带） |
| 网络 | 需能访问 `okx.com` |

---

## 11. 版本历史

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-04-23 | v1.0.0 | 初始版本 |
| 2026-04-23 | v1.2.0 | 路径去硬编码；妙想/QQ Bot 加占位符 |
| 2026-05-11 | **v3.0** | 项目独立化；全币种扫描；四大 Job 职责重定义；Cron 同步实际配置 |
