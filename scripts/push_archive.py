# -*- coding: utf-8 -*-
"""push_archive.py — 推送归档（每轮写时间戳文件 + 覆盖 latest.md）

输入：stdin JSON: {"content" 或 "content_file"(优先，UTF-8 文件交接), "ts": "2026-06-05 21:46:00", "title": "可选标题"}
- ts 必填，决定归档文件名（V2.0 管道传 push_pipeline 的运行时刻 now_ts()）
- 输出：<PROJECT_ROOT>\\reports\\agents\\v2-push-{YYYYMMDD-HHMMSS}.md，
  并以同内容覆盖 v2-push-latest.md。

调用方（V2.0 现役）：scripts/push_pipeline.py 在 qq_push 之前完成归档与内容硬校验；
  归档或硬校验失败时禁止外发，外发失败仍保留已完成归档；
  --no-send 时 --reports-dir 指向 dev 目录，不碰生产 latest.md。
手动调试：中文 JSON 禁走 echo 管道（GBK 坏码），先写 tmp/*.json 再 --json-file，或用 content_file。

退出码：0=成功；2=已归档但内容缺渲染模板指纹（<300 字符 / 缺『第N轮』/ 缺📊 段——T4 存证闸，
        pipeline 将其视为硬校验失败并禁止外发）；其余非0=归档失败
"""

import os as _project_os
from pathlib import Path as _ProjectPath

_PROJECT_ROOT = _ProjectPath(
    _project_os.environ.get("OKX_ROOT")
    or _ProjectPath(__file__).resolve().parents[1]
).resolve()

def _project_path(*parts: str) -> str:
    return str(_PROJECT_ROOT.joinpath(*parts))

import argparse, json, os, re, sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")


def sanitize_text(value: str) -> str:
    """Drop invalid surrogate code points that can appear from PowerShell pipes."""
    return value.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def fail(msg: str, code: int = 2):
    print(f"[push_archive][FAIL] {msg}", file=sys.stderr)
    sys.exit(code)

def read_stdin_text() -> str:
    if hasattr(sys.stdin, "buffer"):
        return sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return sys.stdin.read()


def load_payload(args) -> dict:
    if args.stdin:
        raw = read_stdin_text()
    elif args.json_file:
        with open(args.json_file, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    elif args.json:
        raw = args.json
    else:
        fail("缺少输入：需 --stdin / --json-file / --json 之一")
    raw = sanitize_text(raw)
    try:
        return json.loads(raw)
    except Exception as e:
        fail(f"输入 JSON 解析失败: {e}；含中文/特殊符号时建议先写 <PROJECT_ROOT>\\tmp\\*.json 再用 --json-file")

def parse_stamp(ts: str) -> str:
    # ts 格式: "2026-06-05 21:46:00" → 文件名 20260605-214600
    ts_norm = ts.replace("T", " ").replace("Z", "").split(".")[0].strip()
    parts = ts_norm.split(" ")
    if len(parts) >= 2:
        date_part = parts[0].replace("-", "")
        time_part = parts[1].split("+")[0].split("-")[0]
        time_part_clean = "".join(time_part.split(":")[:3])
        if len(time_part_clean) == 4:
            time_part_clean += "00"
        return f"{date_part}-{time_part_clean}"
    return ts_norm.replace("-", "").replace(":", "").replace(" ", "-")


def extract_cycle(content: str):
    patterns = [
        r"Cycle\s+#?(\d+)",
        r"cycle\s+#?(\d+)",
        r"第\s*(\d+)\s*轮",
    ]
    for pat in patterns:
        m = re.search(pat, content, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def main():
    p = argparse.ArgumentParser(description="推送归档")
    p.add_argument("--reports-dir", default=_project_path('reports', 'agents'))
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stdin", action="store_true")
    g.add_argument("--json-file")
    g.add_argument("--json")
    args = p.parse_args()
    data = load_payload(args)
    content = data.get("content")
    # 2026-06-13: content_file 优先——渲染产物经 UTF-8 文件交接（render --out-file），
    # 绕开 agent exec 捕获 stdout 的 GBK 管道乱码（content 字段经 echo 中转必坏中文）。
    cf = data.get("content_file")
    if cf:
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                content = fh.read()
        except Exception as e:
            fail(f"content_file 读取失败: {cf} ({e})")
    if not content or not isinstance(content, str):
        fail("缺 'content'/'content_file' 字段")
    ts = data.get("ts")
    if not ts or not isinstance(ts, str):
        fail("缺 'ts' 字段")
    # ts 格式解析；文件名必须与 payload ts 一致，禁止用脚本运行时间替代。
    try:
        stamp = parse_stamp(ts)
    except Exception as e:
        fail(f"ts 格式无法解析: {ts} ({e})")
    os.makedirs(args.reports_dir, exist_ok=True)
    out_path = os.path.join(args.reports_dir, f"v2-push-{stamp}.md")
    cycle = data.get("cycle_count") or data.get("cycle") or extract_cycle(content) or stamp
    title = data.get("title", "")
    header = f"# OKX V2.0 Push — Cycle {cycle}\n\n" if title == "" else f"# {title}\n\n"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(content)
        latest_path = os.path.join(args.reports_dir, "v2-push-latest.md")
        with open(latest_path, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(content)
    except Exception as e:
        fail(f"写入失败: {e}")
    # T4 硬闸（2026-06-12 #2295 推送塌缩事故）：归档永远完成（审计底线），
    # 但内容缺渲染模板指纹时 rc=2 提醒 agent 重走 render→validate，不得外发当前内容。
    degraded_reasons = []
    if len(content) < 300:
        degraded_reasons.append(f"内容仅 {len(content)} 字符(<300)")
    if not re.search(r"第\s*\d+\s*轮", content):
        degraded_reasons.append("缺『第N轮』header")
    if "📊" not in content:
        degraded_reasons.append("缺『📊 资产』段")
    result = {"ok": True, "path": out_path, "latest": latest_path, "cycle": cycle, "stamp": stamp,
              "bytes": os.path.getsize(out_path)}
    if degraded_reasons:
        result["degraded"] = True
        result["degraded_reasons"] = degraded_reasons
        print(json.dumps(result, ensure_ascii=False))
        print(f"[push_archive][P2] 内容非渲染模板：{'；'.join(degraded_reasons)}。已归档存证，"
              f"但禁止外发——请回 render_push_report.py 重渲染并过 validate_push_format.py。", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)

if __name__ == "__main__":
    main()
