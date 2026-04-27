#!/usr/bin/env python3
"""Keep an Open WebUI tool JSON file in sync with a Python source file.

Usage examples:
  python sync_tool_json.py
  python sync_tool_json.py --watch
  python sync_tool_json.py --src custom.py --json custom.tool.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
from datetime import datetime
from typing import Any


def _read_json(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _ordered_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in ("id", "name", "description", "content"):
        if key in payload:
            ordered[key] = payload[key]
    for key, value in payload.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _sync_once(
    src: pathlib.Path,
    json_path: pathlib.Path,
    tool_id: str | None,
    tool_name: str | None,
    tool_description: str | None,
) -> bool:
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {src}")

    src_text = src.read_text(encoding="utf-8")
    existing = _read_json(json_path)

    payload: dict[str, Any] = dict(existing)
    payload["id"] = tool_id or existing.get("id") or src.stem
    payload["name"] = tool_name or existing.get("name") or src.stem
    payload["description"] = (
        tool_description
        or existing.get("description")
        or f"Tool generated from {src.name}"
    )
    payload["content"] = src_text

    ordered = _ordered_payload(payload)
    rendered = json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"

    current = json_path.read_text(encoding="utf-8") if json_path.exists() else None
    if current == rendered:
        return False

    json_path.write_text(rendered, encoding="utf-8")
    return True


def _watch(
    src: pathlib.Path,
    json_path: pathlib.Path,
    tool_id: str | None,
    tool_name: str | None,
    tool_description: str | None,
    interval: float,
) -> None:
    print(f"Watching {src} -> {json_path} (interval={interval}s)")

    last_sig: tuple[int, int] | None = None
    while True:
        try:
            stat = src.stat()
            sig = (stat.st_mtime_ns, stat.st_size)
            if sig != last_sig:
                changed = _sync_once(
                    src, json_path, tool_id, tool_name, tool_description
                )
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if changed:
                    print(f"[{stamp}] Synced {json_path.name}")
                else:
                    print(f"[{stamp}] Checked (no changes)")
                last_sig = sig
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except Exception as exc:  # pragma: no cover
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] Error: {exc}")

        time.sleep(interval)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Open WebUI tool JSON content from a Python file."
    )
    parser.add_argument(
        "--src",
        default="local_directory_import.py",
        help="Path to the Python source file.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="local_directory_import.tool.json",
        help="Path to the tool JSON file.",
    )
    parser.add_argument("--id", dest="tool_id", help="Tool id override.")
    parser.add_argument("--name", dest="tool_name", help="Tool name override.")
    parser.add_argument(
        "--description",
        dest="tool_description",
        help="Tool description override.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch source file for changes and keep JSON updated.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Watch polling interval in seconds (default: 1.0).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    src = pathlib.Path(args.src)
    json_path = pathlib.Path(args.json_path)

    if args.watch:
        _watch(
            src,
            json_path,
            args.tool_id,
            args.tool_name,
            args.tool_description,
            args.interval,
        )
        return 0

    changed = _sync_once(
        src,
        json_path,
        args.tool_id,
        args.tool_name,
        args.tool_description,
    )
    if changed:
        print(f"Updated {json_path}")
    else:
        print(f"No changes in {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
