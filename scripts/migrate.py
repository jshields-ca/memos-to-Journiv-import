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


# ── Hashtag extraction & content cleaning ─────────────────────────────────────

HASHTAG_RE = re.compile(r"(?<!\w)#([\w/-]+)")


def extract_hashtags(content: str) -> list[str]:
    """Return all hashtags found in memo content (without the # prefix)."""
    return [m.group(1).lower() for m in HASHTAG_RE.finditer(content)]


def _clean_content(text: str) -> str:
    """
    Normalise whitespace after hashtag removal:
    - Collapse multiple spaces/tabs on a line to one space
    - Collapse 3+ consecutive blank lines to two (one visual paragraph break)
    - Strip leading/trailing whitespace from the whole string
    """
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_hashtag(content: str, tag: str) -> str:
    """Remove a specific #tag and any tag-only line it leaves behind."""
    cleaned = re.sub(rf"(?<!\w)#{re.escape(tag)}\b", "", content)
    # Drop any line that became nothing but whitespace after stripping
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    return _clean_content("\n".join(lines))


def strip_tags_from_content(content: str, tags_to_strip: list[str]) -> str:
    """Remove a list of specific #tags from content in one pass."""
    pattern = "|".join(rf"(?<!\w)#{re.escape(t)}\b" for t in tags_to_strip)
    cleaned = re.sub(pattern, "", content)
    # Drop lines that became nothing but whitespace
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    return _clean_content("\n".join(lines))


def strip_all_hashtags(content: str) -> str:
    """Remove all #hashtags from content and clean up whitespace."""
    cleaned = HASHTAG_RE.sub("", content)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    return _clean_content("\n".join(lines))


def extract_title(content: str) -> tuple[str | None, str]:
    """
    If the content starts with a Markdown H1 heading, extract it as a title
    and return (title, remaining_content). Otherwise return (None, content).
    """
    match = re.match(r"^#\s+(.+?)(?:\n|$)", content.lstrip())
    if match:
        title = match.group(1).strip()
        rest = content[match.end():].strip()
        return title, rest
    return None, content


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    mood_map = {k.lower(): v for k, v in cfg.get("mood_map", {}).items()}
    exclude_tags = {t.lower() for t in cfg.get("exclude_tags", [])}
    return {
        "mood_map": mood_map,
        "exclude_tags": exclude_tags,
        "strip_mood_hashtags": cfg.get("strip_mood_hashtags_from_content", True),
        "strip_all_hashtags": cfg.get("strip_all_hashtags_from_content", False),
    }


# ── Memo → Journiv entry transformation ──────────────────────────────────────

def parse_timestamp(ts: str | None) -> str:
    """Normalise a Memos timestamp to ISO 8601 UTC string."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.isoformat()
    except ValueError:
        return ts


def transform_memo(memo: dict, cfg: dict) -> tuple[dict, list[str]]:
    """
    Convert a single Memos memo dict into a Journiv entry dict.

    Returns (entry_dict, warnings) where warnings is a list of strings
    describing anything that couldn't be fully migrated (e.g. attachments).

    The output shape targets Journiv's native export/import format.
    After running inspect_journiv_export.py, verify these field names
    match your Journiv instance and adjust if needed.
    """
    warnings: list[str] = []
    content = memo.get("content", "")
    hashtags = extract_hashtags(content)
    mood_map: dict[str, str] = cfg["mood_map"]
    exclude_tags: set[str] = cfg["exclude_tags"]

    # Warn about attachments — these are not migrated (text-only)
    attachments = memo.get("attachments", [])
    if attachments:
        names = [a.get("filename", a.get("name", "?")) for a in attachments]
        warnings.append(
            f"Entry from {memo.get('createTime','?')[:10]} has {len(attachments)} "
            f"attachment(s) not migrated: {', '.join(names)}"
        )

    # Determine mood: first hashtag that matches the mood map wins
    mood: str | None = None
    matched_mood_tag: str | None = None
    for tag in hashtags:
        if tag in mood_map:
            mood = mood_map[tag]
            matched_mood_tag = tag
            break

    # Build tag list: keep tags that aren't mood tags and aren't in exclude_tags
    tags = [
        t for t in hashtags
        if t != matched_mood_tag and t not in exclude_tags and t not in mood_map
    ]
    tags = list(dict.fromkeys(tags))  # deduplicate while preserving order

    # Clean content: decide which hashtags to strip from the body
    working_content = content
    if cfg["strip_all_hashtags"]:
        working_content = strip_all_hashtags(working_content)
    else:
        # Always strip mood hashtag and excluded tags; optionally all mood hashtags
        tags_to_strip = list(exclude_tags)
        if cfg["strip_mood_hashtags"] and matched_mood_tag:
            tags_to_strip.append(matched_mood_tag)
        if tags_to_strip:
            working_content = strip_tags_from_content(working_content, tags_to_strip)

    # Extract H1 title if present (native Memos entries often start with "# Title")
    title, body_content = extract_title(working_content)

    # Convert Markdown body → Quill Delta
    quill_content = md_to_quill_delta(body_content if title else working_content)

    entry: dict = {
        "uuid": str(uuid.uuid4()),
        "date": parse_timestamp(memo.get("createTime") or memo.get("create_time")),
        "updated_at": parse_timestamp(memo.get("updateTime") or memo.get("update_time")),
        "content": quill_content,
        "tags": tags,
        "starred": bool(memo.get("pinned", False)),
    }

    if title:
        entry["title"] = title
    if mood:
        entry["mood"] = mood

    return entry, warnings


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

def print_dry_run_report(entries: list[dict], all_warnings: list[str]) -> None:
    mood_counts: dict[str, int] = {}
    no_mood = 0
    tag_set: set[str] = set()
    titled = 0

    for e in entries:
        m = e.get("mood")
        if m:
            mood_counts[m] = mood_counts.get(m, 0) + 1
        else:
            no_mood += 1
        tag_set.update(e.get("tags", []))
        if e.get("title"):
            titled += 1

    print(f"\n{'─' * 60}")
    print(f"  DRY RUN — no files written")
    print(f"{'─' * 60}")
    print(f"  Total entries to migrate:  {len(entries)}")
    print(f"  Entries with a title:      {titled}")

    print(f"\n  Mood distribution:")
    for mood, count in sorted(mood_counts.items(), key=lambda x: -x[1]):
        print(f"    {mood:<20} {count}")
    if no_mood:
        print(f"    {'(no mood)':<20} {no_mood}")

    print(f"\n  Unique tags (excl. mood/excluded):  {len(tag_set)}")
    for t in sorted(tag_set):
        print(f"    #{t}")

    if all_warnings:
        print(f"\n  ⚠  Warnings ({len(all_warnings)}):")
        for w in all_warnings:
            print(f"    • {w}")

    print(f"\n  First 3 entries (preview):")
    for e in entries[:3]:
        date = e["date"][:10]
        mood = e.get("mood", "(none)")
        title = e.get("title", "")
        tags = ", ".join(f"#{t}" for t in e.get("tags", [])) or "(none)"
        text = "".join(
            op["insert"] for op in e["content"]["ops"]
            if isinstance(op.get("insert"), str)
        )[:100].replace("\n", " ").strip()
        print(f"\n    [{date}] mood={mood}  tags={tags}")
        if title:
            print(f"    title={title!r}")
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
    print(f"Loaded {len(cfg['mood_map'])} mood rules, {len(cfg['exclude_tags'])} excluded tags")

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
    all_warnings: list[str] = []
    entries: list[dict] = []
    for memo in raw_memos:
        entry, warnings = transform_memo(memo, cfg)
        entries.append(entry)
        all_warnings.extend(warnings)

    # Sort chronologically
    entries.sort(key=lambda e: e["date"])

    if args.dry_run:
        print_dry_run_report(entries, all_warnings)
        return

    # Print warnings even on full run
    if all_warnings:
        print(f"\n⚠  {len(all_warnings)} warning(s):")
        for w in all_warnings:
            print(f"  • {w}")

    # Build ZIP
    output = args.output
    build_import_zip(entries, output)
    size_kb = Path(output).stat().st_size // 1024
    print(f"\nWrote {len(entries)} entries to: {output}  ({size_kb} KB)")
    print("\nNEXT STEPS:")
    print("  1. Open Journiv → Settings → Import")
    print("  2. Select the ZIP file above")
    print("  3. Verify entry count and spot-check a few entries")
    print("  4. If the import looks wrong, inspect a Journiv export ZIP:")
    print("     python3 scripts/inspect_journiv_export.py journiv_sample_export.zip")
    print("     Then compare the schema to transform_memo() and adjust field names.")


if __name__ == "__main__":
    main()
