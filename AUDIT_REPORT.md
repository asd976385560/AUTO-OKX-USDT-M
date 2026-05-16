# 项目一致性审计报告

> 审计时间：2026-05-11 22:56 UTC+8
> 审计范围：E:\OKX\ 全部文件

---

## 🔴 严重问题

### 1. Cron Payload 路径已指向当前项目

| 位置 | 当前值 | 应为 |
|------|--------|------|
| Job A cron payload | `E:\OKX\scripts\collect_data.py` | `E:\OKX\scripts\collect_data.py` |
| Job E cron payload | `E:\OKX\scripts\collect_slow.py` | `E:\OKX\scripts\collect_slow.py` |
| Job B prompt | `E:\OKX\skill.md` | `E:\OKX\skill.md` |
| Job B prompt | `E:\OKX` | `E:\OKX` |
| Job C prompt | `E:\OKX\skill.md` | `E:\OKX\skill.md` |

**影响**：Cron 实际运行时仍调用旧路径的脚本，新目录 E:\OKX\ 的修改不生效。

**修复**：
1. 如需切换到新路径 → 更新 Cron payload 中的路径
2. 如保持现状 → README 中需说明 E:\OKX\ 仅作备份/分享用途，实际运行仍在旧路径

### 2. README 中 Cron 配置路径不一致

README.md 中 Cron 示例写的是新路径：
```
python E:\OKX\scripts\collect_data.py
```

但实际运行的 Cron 配置仍是旧路径。两者不一致。

---

## 🟡 中等问题

### 3. skill.md 版本历史缺失 v2.1.2 之前的版本

skill.md §13 版本历史只有 v2.0.0、v2.1.2、v3.0，缺少中间版本（v2.1.0、v2.1.1）。

**影响**：不影响运行，但版本历史不完整。

### 4. config.md §5 版本历史只有 v1.x

config.md §11 版本历史只有 v1.0.0、v1.2.0、v2.0，但当前文件头写的是 "OKX v3.0"。

**矛盾点**：文件头写 v3.0，版本历史只到 v2.0。

**修复**：config.md 文件头应改为 "OKX v2.0"，或版本历史补充到 v3.0。

### 5. README 标题写 "v3.0"，但 config.md 文件头写 "v3.0"

实际上 config.md 是 v2.0（版本历史），skill.md 是 v3.0。三者版本号不统一。

| 文件 | 当前版本标识 | 应改为 |
|------|-------------|--------|
| skill.md | v3.0 | ✅ 正确 |
| config.md | v3.0（文件头） | v2.0 |
| README.md | v3.0 | ✅ 项目整体版本 |

**建议**：项目整体版本为 v3.0，但 config.md 自身版本为 v2.0，skill.md 自身版本为 v3.0。

### 6. skill.md §12 相关文件列表缺少 `docs/` 目录文件

skill.md 的 "相关文件" 列表只列了 scripts/ 和 prompts/，缺少 docs/ 下的文件。

### 7. skill.md 和 config.md 中 "妙想 API Key" 明文暴露

config.md §4.4 中妙想 Key 是明文。虽然主人说不需要迁移到环境变量，但这是一个安全隐患。

---

## 🟢 轻微问题

### 8. README.md "版本" 表格缺少中间版本

只列了 v2.1.2 和 v3.0，缺少 v2.1.0、v2.1.1。

### 9. docs/trading-summary.md 交易记录不完整

只有部分字段填充，5 笔交易的具体 PnL 数据缺失。

### 10. prompts/jobb-prompt.md 中 skill.md 路径已同步

jobb-prompt.md 中引用了当前路径：
```
E:\OKX\skill.md
```

如果 Cron 切换到新路径，prompt 中的路径也需要同步更新。

---

## ✅ 一致性检查通过项

| 检查项 | 结果 |
|--------|------|
| 项目根目录引用 | ✅ E:\OKX 一致 |
| 数据库目录 | ✅ E:\OKX\db 一致 |
| Cron 表达式 | ✅ A:1,16,31,46 / E:10 / B:5,20,35,50 / C:30 0 一致 |
| 四大 Job 职责定义 | ✅ skill.md / config.md / README.md 一致 |
| 风控硬上限数值 | ✅ 全部一致 |
| 脚本文件名 | ✅ 8 个核心脚本一致 |
| 外部数据源配置 | ✅ FRED/DefiLlama/CoinGecko/妙想一致 |
| 存储模式说明 | ✅ 增量追加一致 |
| 全币种扫描要求 | ✅ skill.md / prompts 一致 |
| 数据流架构 | ✅ 全部文档一致 |

---

## 📋 修复清单

### 必须修复（影响运行）

- [ ] **#1** Cron payload 路径：决定是切换新路径还是标注旧路径运行
- [ ] **#2** README Cron 示例与实际运行路径一致性

### 建议修复（影响体验）

- [ ] **#4** config.md 版本号：文件头改为 "v2.0" 或版本历史补充
- [ ] **#10** prompts/jobb-prompt.md 中的 skill.md 路径

### 可选修复（不影响运行）

- [ ] **#3** skill.md 版本历史补充中间版本
- [ ] **#6** skill.md 相关文件列表补充 docs/
- [ ] **#7** API Key 安全（主人已确认不改）
- [ ] **#8** README 版本表格补充
- [ ] **#9** trading-summary.md 补充完整交易记录

---

## 建议

**当前状态**：E:\OKX\ 是一个完整的项目备份/分享目录，但实际运行的 Cron 仍指向旧路径。

**两种方案**：

**方案 A**（推荐）：切换 Cron 到 E:\OKX\
- 更新所有 Cron payload 路径为新路径
- 更新 prompts 中的路径引用
- 此后旧项目目录可废弃

**方案 B**：保持现状
- E:\OKX\ 仅作为分享/备份/文档用途
- 实际运行仍在旧路径
- README 需注明此点

主人请选择方案。
