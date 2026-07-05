#!/usr/bin/env python3
"""
Export vault Company-Registry.yaml → data/company_registry.json for screener.

Usage (from repo root):
  python scripts/export_registry.py --vault /path/to/your/obsidian-vault
  OBSIDIAN_VAULT_PATH=/path/to/vault python scripts/export_registry.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Install PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT_ROOT / "data" / "company_registry.json"


def _default_vault() -> Path | None:
    raw = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
    return Path(raw) if raw else None


def export_registry(vault_path: Path, out_path: Path) -> dict:
    yaml_path = vault_path / "wiki" / "investing" / "Company-Registry.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"Registry not found: {yaml_path}")

    with open(yaml_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    payload = {
        "meta": data.get("meta") or {},
        "companies": data.get("companies") or {},
        "exported_from": str(yaml_path),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


def main():
    parser = argparse.ArgumentParser(description="Export vault Company-Registry to JSON")
    parser.add_argument("--vault", type=Path, default=None, help="Obsidian vault root (or set OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    vault = args.vault or _default_vault()
    if vault is None:
        print(
            "Error: pass --vault or set OBSIDIAN_VAULT_PATH in .env",
            file=sys.stderr,
        )
        sys.exit(1)
    payload = export_registry(vault, args.out)
    n = len(payload.get("companies") or {})
    print(f"Exported {n} companies → {args.out}")


if __name__ == "__main__":
    main()