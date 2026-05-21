#!/usr/bin/env python3
"""Sync or check the Codex marketplace plugin payload from root sources."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "plugins" / "agent-channels"

PATHS = [
    ".codex-plugin",
    "skills",
    "bin",
    "src",
    "package.json",
]


def iter_files(path: Path) -> set[Path]:
    return {
        p.relative_to(path)
        for p in path.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }


def copy_path(rel: str) -> None:
    src = ROOT / rel
    dst = TARGET / rel
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def check_synced() -> list[str]:
    errors: list[str] = []
    for rel in PATHS:
        src = ROOT / rel
        dst = TARGET / rel
        if not dst.exists():
            errors.append(f"missing {TARGET.relative_to(ROOT) / rel}")
            continue
        if src.is_dir():
            src_files = iter_files(src)
            dst_files = iter_files(dst)
            for missing in sorted(src_files - dst_files):
                errors.append(f"missing {TARGET.relative_to(ROOT) / rel / missing}")
            for extra in sorted(dst_files - src_files):
                errors.append(f"extra {TARGET.relative_to(ROOT) / rel / extra}")
            for common in sorted(src_files & dst_files):
                if (src / common).read_bytes() != (dst / common).read_bytes():
                    errors.append(f"changed {TARGET.relative_to(ROOT) / rel / common}")
        elif src.read_bytes() != dst.read_bytes():
            errors.append(f"changed {TARGET.relative_to(ROOT) / rel}")
    return errors


def sync() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for rel in PATHS:
        copy_path(rel)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync or check plugins/agent-channels against root sources."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="only verify that the generated payload is current",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.check:
        sync()

    errors = check_synced()
    if errors:
        for error in errors:
            print(f"sync-codex-plugin: {error}")
        raise SystemExit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
