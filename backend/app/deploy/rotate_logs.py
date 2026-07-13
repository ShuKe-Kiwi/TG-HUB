"""One-shot CLI for P6-Deploy-3C local rotation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from app.config import load_settings
from app.deploy.rotation_engine import RotationEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-hub-rotate-logs")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = RotationEngine(load_settings()).run(dry_run=args.dry_run)
    print(result.model_dump_json())
    if result.status == "pass":
        return 0
    if result.status == "partial":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
