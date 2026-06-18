#!/usr/bin/env python3
"""
Inspect a Journiv export ZIP to discover the Journal.json schema.

Run this after creating a few test entries in Journiv and exporting them.
It prints the structure of the export so the migration script can target
the correct field names.

Usage:
    python3 scripts/inspect_journiv_export.py journiv_sample_export.zip

Output:
    - Pretty-printed first entry as example
    - List of all top-level keys and their types
    - Writes journiv_entry_schema.json to the current directory
"""

import json
import sys
import zipfile
from pathlib import Path


def find_journal_json(zf: zipfile.ZipFile) -> str | None:
    """Return the data/entry file inside the ZIP. Journiv uses data.json (beta.17+)."""
    for name in zf.namelist():
        if name.lower() in ("data.json", "journal.json"):
            return name
    # Fallback: any .json at root level
    for name in zf.namelist():
        if name.lower().endswith(".json") and "/" not in name:
            return name
    return None


def describe_value(val: object, indent: int = 0) -> str:
    prefix = "  " * indent
    if isinstance(val, dict):
        lines = [f"{prefix}{{"]
        for k, v in val.items():
            lines.append(f"{prefix}  {k!r}: {describe_value(v, indent + 1).lstrip()}")
        lines.append(f"{prefix}}}")
        return "\n".join(lines)
    if isinstance(val, list):
        if not val:
            return f"{prefix}[]"
        return f"{prefix}[{type(val[0]).__name__}, ...] (len={len(val)})"
    return f"{prefix}{type(val).__name__} = {val!r}"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/inspect_journiv_export.py <export.zip>")
        sys.exit(1)

    zip_path = Path(sys.argv[1])
    if not zip_path.exists():
        print(f"ERROR: file not found: {zip_path}")
        sys.exit(1)

    with zipfile.ZipFile(zip_path) as zf:
        print("Files inside ZIP:")
        for name in zf.namelist():
            info = zf.getinfo(name)
            print(f"  {name}  ({info.file_size:,} bytes)")

        # Journiv exports as data.json (beta.17+); older versions used Journal.json
        journal_name = find_journal_json(zf)
        if not journal_name:
            print("\nERROR: No data.json or Journal.json found inside the ZIP.")
            print("Contents:", zf.namelist())
            sys.exit(1)

        print(f"\nReading: {journal_name}")
        data = json.loads(zf.read(journal_name).decode("utf-8"))

    # Top-level structure
    print("\n── Top-level structure ─────────────────────────────────────")
    if isinstance(data, dict):
        for k, v in data.items():
            print(f"  {k!r}: {type(v).__name__}", end="")
            if isinstance(v, list):
                print(f" (len={len(v)})", end="")
            print()
        entries = None
        for key in ("entries", "memos", "items", "journal_entries"):
            if key in data and isinstance(data[key], list):
                entries = data[key]
                print(f"\nEntry array key: {key!r}")
                break
        if entries is None and isinstance(data, dict):
            # Maybe the root IS the list — look for the biggest list value
            lists = {k: v for k, v in data.items() if isinstance(v, list)}
            if lists:
                key = max(lists, key=lambda k: len(lists[k]))
                entries = lists[key]
                print(f"\nAssuming entry array key: {key!r} (largest list, len={len(entries)})")
    elif isinstance(data, list):
        entries = data
        print("  Root is a JSON array.")
    else:
        print(f"  Unexpected root type: {type(data).__name__}")
        sys.exit(1)

    if not entries:
        print("\nNo entries found in the export.")
        sys.exit(0)

    # Schema from first entry
    first = entries[0]
    print(f"\n── First entry fields ({len(entries)} total entries) ───────────────")
    for k, v in first.items():
        type_name = type(v).__name__
        if isinstance(v, dict):
            print(f"  {k!r}: dict  keys={list(v.keys())}")
        elif isinstance(v, list):
            item_type = type(v[0]).__name__ if v else "empty"
            print(f"  {k!r}: list[{item_type}]  len={len(v)}")
        else:
            print(f"  {k!r}: {type_name} = {v!r}")

    print("\n── First entry (pretty printed) ───────────────────────────")
    print(json.dumps(first, indent=2, ensure_ascii=False, default=str))

    # Save schema
    schema = {
        "source_file": str(zip_path),
        "top_level_type": type(data).__name__,
        "entry_count": len(entries),
        "entry_fields": {
            k: {
                "type": type(v).__name__,
                "example": v if not isinstance(v, (dict, list)) else None,
                "keys": list(v.keys()) if isinstance(v, dict) else None,
                "item_type": type(v[0]).__name__ if isinstance(v, list) and v else None,
            }
            for k, v in first.items()
        },
        "first_entry_raw": first,
    }
    schema_path = Path("journiv_entry_schema.json")
    schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False, default=str))
    print(f"\nSchema written to: {schema_path}")
    print("\nNEXT STEP: Share journiv_entry_schema.json so the migration script")
    print("can be tuned to match the exact import format.")


if __name__ == "__main__":
    main()
