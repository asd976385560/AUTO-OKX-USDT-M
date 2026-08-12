# -*- coding: utf-8 -*-
"""Validate lifecycle ownership for every top-level Python/PowerShell script."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
DEFAULT_MANIFEST = SCRIPTS / "lifecycle.json"
ALLOWED_STATUSES = {
    "runtime",
    "helper",
    "manual-maintenance",
    "research",
    "migration-pending",
    "migration-complete",
    "retired",
}
REQUIRED_GROUP_FIELDS = {
    "status",
    "invocation",
    "write_scope",
    "default_mode",
    "replacement",
    "last_verified",
    "paths",
}


def tracked_scripts(root: Path = SCRIPTS) -> set[str]:
    """Return publishable top-level scripts, including intentional new files.

    Ignored host-only utilities may coexist in an operator checkout.  They are
    not part of the public release and must not force entries into the public
    lifecycle manifest, so the repository index/ignore rules are authoritative.
    """
    root = root.resolve()
    repo_root = root.parent
    result = subprocess.run(
        ["git", "-c", f"safe.directory={repo_root.as_posix()}",
         "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", root.name],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed; lifecycle scope is unknown")
    names: set[str] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8").replace("\\", "/"))
        if (len(relative.parts) == 2 and relative.parts[0] == root.name
                and relative.suffix.lower() in {".py", ".ps1"}
                and (repo_root / relative).is_file()):
            names.add(relative.name)
    return names


def validate_manifest(
    manifest: dict,
    scripts: set[str],
) -> list[str]:
    errors: list[str] = []
    if manifest.get("version") != 1:
        errors.append("manifest.version 必须为 1")
    groups = manifest.get("groups")
    if not isinstance(groups, list):
        return errors + ["manifest.groups 必须是 list"]

    owners: dict[str, str] = {}
    for index, group in enumerate(groups):
        label = str(group.get("name") or f"groups[{index}]")
        missing_fields = sorted(REQUIRED_GROUP_FIELDS - set(group))
        if missing_fields:
            errors.append(f"{label}: 缺字段 {','.join(missing_fields)}")
            continue
        status = group.get("status")
        if status not in ALLOWED_STATUSES:
            errors.append(f"{label}: 非法 status={status!r}")
        paths = group.get("paths")
        if not isinstance(paths, list) or not paths:
            errors.append(f"{label}: paths 必须是非空 list")
            continue
        for raw_path in paths:
            name = str(raw_path)
            if Path(name).name != name:
                errors.append(f"{label}: 仅允许 scripts 顶层文件名: {name}")
                continue
            if name in owners:
                errors.append(
                    f"{name}: 同时属于 {owners[name]} 与 {label}")
            owners[name] = label
            if (
                status in {"migration-complete", "retired"}
                and name in scripts
            ):
                errors.append(
                    f"{name}: {status} 文件不得继续留在 scripts 顶层")

    missing = sorted(scripts - set(owners))
    extra = sorted(set(owners) - scripts)
    if missing:
        errors.append("未登记脚本: " + ", ".join(missing))
    if extra:
        errors.append("登记但文件不存在: " + ", ".join(extra))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="检查 scripts 顶层文件生命周期登记")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    scripts = tracked_scripts(args.manifest.parent)
    errors = validate_manifest(manifest, scripts)
    result = {
        "ok": not errors,
        "manifest": str(args.manifest),
        "tracked_scripts": len(scripts),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"script lifecycle: {'PASS' if result['ok'] else 'FAIL'} "
            f"tracked={len(scripts)} errors={len(errors)}")
        for error in errors:
            print(f"  - {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
