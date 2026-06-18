#!/usr/bin/env python3
"""
Memos → Journiv migration script.

Reads a Memos JSON export (or live API) and produces a Journiv-compatible
import ZIP file. The ZIP contains a Journal.json with all entries transformed
to Journiv's format.

This script is READ-ONLY with respect to Memos. It never writes to or
modifies the Memos database.

Usage:
    # Dry run — preview mapping, no files written
    python3 scripts/migrate.py --input memos_export.json --dry-run

    # Full migration
    python3 scripts/migrate.py --input memos_export.json

    # Custom output path
    python3 scripts/migrate.py --input memos_export.json --output my_import.zip

    # Live API mode
    python3 scripts/migrate.py --api-url http://localhost:5230 --api-token YOUR_TOKEN

    # Custom mood mapping config
    python3 scripts/migrate.py --input memos_export.json --config config/mood_mapping.yaml

    # After running inspect_journiv_export.py, adapt the schema if needed:
    python3 scripts/migrate.py --input memos_export.json --schema journiv_entry_schema.json
"""

import argparse
import json
import re
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import yaml

# Allow running from repo root or from scripts/
sys.path.insert(0, str(Path(__file__).parent))
from lib.memos_reader import load_from_file, load_from_api
from lib.quill_delta import md_to_quill_delta


# ── Hashtag extraction ────────────────────────────────────────────────────────

HASHTAG_RE = re.compile(r"(?<!\w)#([\w/-]+)")


def extract_hashtags(content: str) -> list[str]:
    """Return all hashtags found in memo content (without the # prefix)."""
    return [m.group(1).lower() for m in HASHTAG_RE.finditer(content)]


def _collapse_spaces(text: str) -> str:
    """Collapse multiple spaces into one and clean up trailing spaces on lines."""
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def strip_hashtag(content: str, tag: str) -> str:
    """Remove a specific #tag from content and clean up extra whitespace."""
    cleaned = re.sub(rf"(?<!\w)#{re.escape(tag)}\b", "", content)
    return _collapse_spaces(cleaned)


def strip_all_hashtags(content: str) -> str:
    """Remove all #hashtags from content and clean up extra whitespace."""
    return _collapse_spaces(HASHTAG_RE.sub("", content))


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    mood_map = {k.lower(): v for k, v in cfg.get("mood_map", {}).items()}
    return {
        "mood_map": mood_map,
        "strip_mood_hashtags": cfg.get("strip_mood_hashtags_from_content", True),
        "strip_all_hashtags": cfg.get("strip_all_hashtags_from_content", False),
    }


# ── Memo → Journiv entry transformation ──────────────────────────────────────

def parse_timestamp(ts: str | None) -> str:
    """Normalise a Memos timestamp to ISO 8601 UTC string."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    # Already ISO 8601 — just ensure UTC marker is present
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        return ts


def transform_memo(memo: dict, cfg: dict) -> dict:
    """
    Convert a single Memos memo dict into a Journiv entry dict.

    The output shape targets Journiv's native export/import format.
    After running inspect_journiv_export.py, verify these field names
    match your Journiv instance and adjust if needed.
    """
    content = memo.get("content", "")
    hashtags = extract_hashtags(content)
    mood_map: dict[str, str] = cfg["mood_map"]

    # Determine mood: first hashtag that matches the mood map wins
    mood: str | None = None
    matched_mood_tag: str | None = None
    for tag in hashtags:
        base_tag = tag.split("/")[0]  # handle memos/subtag style
        if base_tag in mood_map:
            mood = mood_map[base_tag]
            matched_mood_tag = tag
            break

    # Build tag list: hashtags that didn't map to a mood
    tags = [t for t in hashtags if t != matched_mood_tag]
    # Use only the top-level part of hierarchical tags (e.g. "work/project" → "work/project")
    tags = list(dict.fromkeys(tags))  # deduplicate while preserving order

    # Optionally strip hashtags from content
    working_content = content
    if cfg["strip_all_hashtags"]:
        working_content = strip_all_hashtags(working_content)
    elif cfg["strip_mood_hashtags"] and matched_mood_tag:
        working_content = strip_hashtag(working_content, matched_mood_tag)

    # Convert Markdown → Quill Delta
    quill_content = md_to_quill_delta(working_content)

    entry = {
        "uuid": str(uuid.uuid4()),
        "date": parse_timestamp(memo.get("createTime") or memo.get("create_time")),
        "updated_at": parse_timestamp(memo.get("updateTime") or memo.get("update_time")),
        "content": quill_content,
        "tags": tags,
        "starred": bool(memo.get("pinned", False)),
    }

    if mood:
        entry["mood"] = mood

    return entry


# ── ZIP assembly ──────────────────────────────────────────────────────────────

def build_import_zip(entries: list[dict], output_path: str) -> None:
    """Write a Journiv-compatible import ZIP to output_path."""
    journal = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(entries),
        "entries": entries,
    }
    journal_json = json.dumps(journal, indent=2, ensure_ascii=False).encode("utf-8")

    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Journal.json", journal_json)
    buf.seek(0)

    Path(output_path).write_bytes(buf.read())


# ── Dry-run report ────────────────────────────────────────────────────────────

def print_dry_run_report(entries: list[dict], cfg: dict) -> None:
    mood_counts: dict[str, int] = {}
    no_mood = 0
    tag_set: set[str] = set()

    for e in entries:
        m = e.get("mood")
        if m:
            mood_counts[m] = mood_counts.get(m, 0) + 1
        else:
            no_mood += 1
        tag_set.update(e.get("tags", []))

    print(f"\n{'─' * 60}")
    print(f"  DRY RUN — no files written")
    print(f"{'─' * 60}")
    print(f"  Total entries to migrate:  {len(entries)}")
    print(f"\n  Mood distribution:")
    for mood, count in sorted(mood_counts.items(), key=lambda x: -x[1]):
        print(f"    {mood:<20} {count}")
    print(f"    {'(no mood)':<20} {no_mood}")
    print(f"\n  Unique tags found:  {len(tag_set)}")
    if tag_set:
        for t in sorted(tag_set)[:20]:
            print(f"    #{t}")
        if len(tag_set) > 20:
            print(f"    ... and {len(tag_set) - 20} more")
    print(f"\n  First 3 entries (preview):")
    for e in entries[:3]:
        date = e["date"][:10]
        mood = e.get("mood", "(none)")
        tags = ", ".join(f"#{t}" for t in e.get("tags", [])) or "(none)"
        # Show first 80 chars of plain text from Quill ops
        text = "".join(
            op["insert"] for op in e["content"]["ops"]
            if isinstance(op.get("insert"), str)
        )[:80].replace("\n", " ").strip()
        print(f"\n    [{date}] mood={mood}  tags={tags}")
        print(f"    {text!r}")
    print(f"\n{'─' * 60}")
    print("  Run without --dry-run to produce the import ZIP.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Migrate Memos entries to a Journiv import ZIP."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", "-i", metavar="FILE",
                        help="Path to a Memos JSON export file")
    source.add_argument("--api-url", metavar="URL",
                        help="Memos instance URL for live API export (e.g. http://localhost:5230)")

    parser.add_argument("--api-token", metavar="TOKEN",
                        help="Memos access token (required with --api-url)")
    parser.add_argument("--output", "-o", metavar="FILE",
                        default="memos_to_journiv_import.zip",
                        help="Output ZIP file path (default: memos_to_journiv_import.zip)")
    parser.add_argument("--config", "-c", metavar="FILE",
                        default="config/mood_mapping.yaml",
                        help="Mood mapping config file (default: config/mood_mapping.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the migration without writing any files")
    parser.add_argument("--include-archived", action="store_true",
                        help="Include archived memos (excluded by default)")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config file not found: {config_path}")
        print("Expected: config/mood_mapping.yaml (run from repo root)")
        sys.exit(1)
    cfg = load_config(str(config_path))
    print(f"Loaded mood mapping: {len(cfg['mood_map'])} hashtag rules from {config_path}")

    # Load memos
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"ERROR: input file not found: {input_path}")
            sys.exit(1)
        print(f"Reading memos from: {input_path}")
        raw_memos = load_from_file(str(input_path))
    else:
        if not args.api_token:
            print("ERROR: --api-token is required when using --api-url")
            sys.exit(1)
        print(f"Fetching memos from API: {args.api_url}")
        raw_memos = list(load_from_api(args.api_url, args.api_token))

    print(f"Loaded {len(raw_memos)} memos")

    # Filter archived unless requested
    if not args.include_archived:
        before = len(raw_memos)
        raw_memos = [
            m for m in raw_memos
            if m.get("state", "NORMAL").upper() != "ARCHIVED"
            and m.get("rowStatus", "NORMAL").upper() != "ARCHIVED"
        ]
        skipped = before - len(raw_memos)
        if skipped:
            print(f"Skipped {skipped} archived memos (use --include-archived to include them)")

    # Transform
    entries = [transform_memo(m, cfg) for m in raw_memos]
    # Sort chronologically
    entries.sort(key=lambda e: e["date"])

    if args.dry_run:
        print_dry_run_report(entries, cfg)
        return

    # Build ZIP
    output = args.output
    build_import_zip(entries, output)
    size_kb = Path(output).stat().st_size // 1024
    print(f"\nWrote {len(entries)} entries to: {output}  ({size_kb} KB)")
    print("\nNEXT STEPS:")
    print("  1. Open Journiv → Settings → Import")
    print("  2. Select the ZIP file above")
    print("  3. Verify entry count and spot-check a few entries")
    print("  4. If the import looks wrong, check journiv_entry_schema.json")
    print("     (from scripts/inspect_journiv_export.py) and adjust the schema")
    print("     in transform_memo() to match your Journiv instance's format.")


if __name__ == "__main__":
    main()
