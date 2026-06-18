#!/usr/bin/env python3
"""
Memos → Journiv migration script.

Reads a Memos JSON export (or live API) and produces a Journiv-compatible
import ZIP (data.json) using the exact format from Journiv's own exports.

This script is READ-ONLY with respect to Memos. It never writes to or
modifies the Memos database. Journiv is the throwaway side — wipe it with
`docker compose down -v && docker compose up -d` and re-import freely.

Usage:
    # Dry run — preview mapping, no files written
    python3 scripts/migrate.py --input memos_export.json \\
        --reference-export journiv_reference.zip --dry-run

    # Full migration
    python3 scripts/migrate.py --input memos_export.json \\
        --reference-export journiv_reference.zip

    # Live API mode
    python3 scripts/migrate.py --api-url http://localhost:5230 \\
        --api-token YOUR_TOKEN --reference-export journiv_reference.zip

    # Custom output path
    python3 scripts/migrate.py --input memos_export.json \\
        --reference-export journiv_reference.zip --output my_import.zip
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
from zoneinfo import ZoneInfo

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from lib.memos_reader import load_from_file, load_from_api
from lib.quill_delta import md_to_quill_delta


# ── Hashtag extraction & content cleaning ─────────────────────────────────────

HASHTAG_RE = re.compile(r"(?<!\w)#([\w/-]+)")


def extract_hashtags(content: str) -> list[str]:
    return [m.group(1).lower() for m in HASHTAG_RE.finditer(content)]


def _clean_content(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_tags_from_content(content: str, tags_to_strip: list[str]) -> str:
    """Remove specific #tags from content and clean up any blank lines left behind."""
    pattern = "|".join(rf"(?<!\w)#{re.escape(t)}\b" for t in tags_to_strip)
    cleaned = re.sub(pattern, "", content)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    return _clean_content("\n".join(lines))


def strip_all_hashtags(content: str) -> str:
    cleaned = HASHTAG_RE.sub("", content)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    return _clean_content("\n".join(lines))


def extract_title(content: str) -> tuple[str | None, str]:
    """Pull an H1 heading from the start of content into a separate title field."""
    match = re.match(r"^#\s+(.+?)(?:\n|$)", content.lstrip())
    if match:
        return match.group(1).strip(), content[match.end():].strip()
    return None, content


# ── Reference export loading ──────────────────────────────────────────────────

def load_reference_export(zip_path: str) -> dict:
    """
    Read data.json from a Journiv export ZIP.
    Returns the parsed dict containing mood_definitions, activities, journals, etc.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        json_file = next((n for n in names if n.lower() == "data.json"), None)
        if not json_file:
            print(f"ERROR: No data.json found in {zip_path}. Contents: {names}")
            sys.exit(1)
        return json.loads(zf.read(json_file).decode("utf-8"))


def build_lookup_tables(ref: dict) -> tuple[dict, dict, str]:
    """
    Extract mood/activity name→UUID lookups and the journal external_id
    from a reference export.

    Returns:
        mood_by_name:     {"Good": {"external_id": "...", "name": "Good"}, ...}
        activity_by_name: {"Work": {"external_id": "...", "name": "Work"}, ...}
        journal_id:       "ffeb4e90-..."
    """
    mood_by_name = {m["name"]: m for m in ref.get("mood_definitions", [])}
    activity_by_name = {a["name"]: a for a in ref.get("activities", [])}

    journals = ref.get("journals", [])
    if not journals:
        print("ERROR: No journals found in reference export.")
        sys.exit(1)
    journal_id = journals[0]["external_id"]

    return mood_by_name, activity_by_name, journal_id


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return {
        "user_timezone": cfg.get("user_timezone", "UTC"),
        "mood_map": {k.lower(): v for k, v in cfg.get("mood_map", {}).items()},
        "activity_map": {k.lower(): v for k, v in cfg.get("activity_map", {}).items()},
        "exclude_tags": {t.lower() for t in cfg.get("exclude_tags", [])},
        "strip_mood_hashtags": cfg.get("strip_mood_hashtags_from_content", True),
        "strip_activity_hashtags": cfg.get("strip_activity_hashtags_from_content", True),
        "strip_all_hashtags": cfg.get("strip_all_hashtags_from_content", False),
    }


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def parse_utc_ts(ts: str | None) -> datetime:
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def to_journiv_ts(dt: datetime) -> str:
    """Format as ISO 8601 UTC string matching Journiv's export style."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(dt: datetime, tz_name: str) -> str:
    """Return the local calendar date string (YYYY-MM-DD) for a UTC datetime."""
    try:
        local_dt = dt.astimezone(ZoneInfo(tz_name))
        return local_dt.date().isoformat()
    except Exception:
        return dt.date().isoformat()


# ── Quill Delta helpers ───────────────────────────────────────────────────────

def delta_to_plain_text(delta: dict) -> str:
    return "".join(
        op["insert"] for op in delta.get("ops", [])
        if isinstance(op.get("insert"), str)
    )


def word_count(text: str) -> int:
    return len(text.split())


# ── Memo → Journiv moment transformation ─────────────────────────────────────

def transform_memo(
    memo: dict,
    cfg: dict,
    mood_by_name: dict,
    activity_by_name: dict,
    journal_id: str,
) -> tuple[dict, list[str]]:
    """
    Convert a Memos memo into a Journiv moment dict.

    Returns (moment_dict, warnings).
    """
    warnings: list[str] = []
    content = memo.get("content", "")
    hashtags = extract_hashtags(content)
    mood_map: dict[str, str] = cfg["mood_map"]
    activity_map: dict[str, str] = cfg["activity_map"]
    exclude_tags: set[str] = cfg["exclude_tags"]
    tz_name: str = cfg["user_timezone"]

    # ── Attachments warning ──
    attachments = memo.get("attachments", [])
    if attachments:
        names = [a.get("filename", a.get("name", "?")) for a in attachments]
        warnings.append(
            f"Entry from {memo.get('createTime','?')[:10]} has {len(attachments)} "
            f"attachment(s) not migrated: {', '.join(names)}"
        )

    # ── Mood ──
    mood_name: str | None = None
    matched_mood_tag: str | None = None
    for tag in hashtags:
        if tag in mood_map:
            mood_name = mood_map[tag]
            matched_mood_tag = tag
            break

    mood_def = mood_by_name.get(mood_name) if mood_name else None
    if mood_name and not mood_def:
        warnings.append(
            f"Mood {mood_name!r} not found in Journiv reference export "
            f"(entry {memo.get('createTime','?')[:10]}). Entry will have no mood."
        )
        mood_name = None

    # ── Activities ──
    activity_tags: list[str] = []
    activity_defs: list[dict] = []
    unknown_activity_tags: list[str] = []
    for tag in hashtags:
        if tag == matched_mood_tag or tag in exclude_tags or tag in mood_map:
            continue
        if tag in activity_map:
            act_name = activity_map[tag]
            act_def = activity_by_name.get(act_name)
            if act_def:
                # Deduplicate: same activity may map from multiple tags (sleep/sleeping/bedtime)
                if not any(a["external_id"] == act_def["external_id"] for a in activity_defs):
                    activity_defs.append(act_def)
                    activity_tags.append(tag)
            else:
                unknown_activity_tags.append(tag)

    if unknown_activity_tags:
        warnings.append(
            f"Activity tag(s) {unknown_activity_tags} not found in Journiv reference export. "
            f"Create them in Journiv and re-export a new reference ZIP."
        )

    # ── Remaining plain tags ──
    mapped_tags = {matched_mood_tag} | set(activity_tags) | exclude_tags | set(mood_map.keys())
    plain_tags = [t for t in hashtags if t not in mapped_tags]
    plain_tags = list(dict.fromkeys(plain_tags))  # deduplicate, preserve order

    # ── Content cleaning ──
    working_content = content
    if cfg["strip_all_hashtags"]:
        working_content = strip_all_hashtags(working_content)
    else:
        tags_to_strip = list(exclude_tags)
        if cfg["strip_mood_hashtags"] and matched_mood_tag:
            tags_to_strip.append(matched_mood_tag)
        if cfg["strip_activity_hashtags"]:
            tags_to_strip.extend(activity_tags)
        if tags_to_strip:
            working_content = strip_tags_from_content(working_content, tags_to_strip)

    # ── Title extraction ──
    title, body_content = extract_title(working_content)
    content_for_delta = body_content if title else working_content

    # ── Quill Delta + plain text ──
    delta = md_to_quill_delta(content_for_delta)
    plain_text = delta_to_plain_text(delta)

    # ── Timestamps ──
    create_dt = parse_utc_ts(memo.get("createTime") or memo.get("create_time"))
    update_dt = parse_utc_ts(memo.get("updateTime") or memo.get("update_time"))
    create_ts = to_journiv_ts(create_dt)
    update_ts = to_journiv_ts(update_dt)
    local_date_str = local_date(create_dt, tz_name)

    # ── mood_activity array ──
    # Journiv stores mood and activities together in this array, mood first.
    mood_activity: list[dict] = []
    if mood_def:
        mood_activity.append({
            "mood_name": mood_def["name"],
            "activity_name": None,
            "mood_external_id": mood_def["external_id"],
            "activity_external_id": None,
        })
    for act_def in activity_defs:
        mood_activity.append({
            "mood_name": None,
            "activity_name": act_def["name"],
            "mood_external_id": None,
            "activity_external_id": act_def["external_id"],
        })

    moment_id = str(uuid.uuid4())
    entry_id = str(uuid.uuid4())

    moment = {
        "logged_at_utc": create_ts,
        "logged_date_tz": local_date_str,
        "logged_timezone": tz_name,
        "note": None,
        "location_json": None,
        "latitude": None,
        "longitude": None,
        "weather_json": None,
        "weather_summary": None,
        "is_pinned": bool(memo.get("pinned", False)),
        "prompt_text": None,
        "tags": plain_tags,
        "people_external_ids": [],
        "primary_mood_name": mood_def["name"] if mood_def else None,
        "primary_mood_external_id": mood_def["external_id"] if mood_def else None,
        "mood_activity": mood_activity,
        "media": [],
        "entry": {
            "title": title,
            "content_delta": delta,
            "content_plain_text": plain_text,
            "word_count": word_count(plain_text),
            "is_draft": False,
            "import_metadata": None,
            "journal_external_id": journal_id,
            "created_at": create_ts,
            "updated_at": update_ts,
            "external_id": entry_id,
        },
        "created_at": create_ts,
        "updated_at": update_ts,
        "external_id": moment_id,
    }

    return moment, warnings


# ── data.json assembly ────────────────────────────────────────────────────────

def build_import_data(moments: list[dict], ref: dict) -> dict:
    """
    Assemble the full data.json structure by combining generated moments
    with the static reference data (mood definitions, activities, journal, etc.).
    """
    return {
        "export_version": ref.get("export_version", "1.5"),
        "export_date": to_journiv_ts(datetime.now(timezone.utc)),
        "app_version": ref.get("app_version", "0.1.0-beta.23"),
        "user_email": ref.get("user_email", ""),
        "user_name": ref.get("user_name", ""),
        "user_settings": ref.get("user_settings"),
        "journals": ref.get("journals", []),
        "mood_definitions": ref.get("mood_definitions", []),
        "mood_preferences": ref.get("mood_preferences", []),
        "mood_groups": ref.get("mood_groups", []),
        "mood_group_links": ref.get("mood_group_links", []),
        "mood_group_preferences": ref.get("mood_group_preferences", []),
        "activities": ref.get("activities", []),
        "activity_groups": ref.get("activity_groups", []),
        "people": [],
        "person_groups": [],
        "goal_categories": [],
        "goals": [],
        "goal_logs": [],
        "goal_manual_logs": [],
        "moments": moments,
        "stats": {
            "journal_count": len(ref.get("journals", [])),
            "entry_count": len(moments),
            "media_count": 0,
            "mood_count": len(ref.get("mood_definitions", [])),
            "mood_group_count": len(ref.get("mood_groups", [])),
            "activity_count": len(ref.get("activities", [])),
            "activity_group_count": len(ref.get("activity_groups", [])),
            "people_count": 0,
            "person_group_count": 0,
            "goal_count": 0,
            "goal_category_count": 0,
            "goal_log_count": 0,
            "export_size_estimate": None,
        },
    }


def build_import_zip(data: dict, output_path: str) -> None:
    data_json = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("data.json", data_json)
    buf.seek(0)
    Path(output_path).write_bytes(buf.read())


# ── Dry-run report ────────────────────────────────────────────────────────────

def print_dry_run_report(moments: list[dict], all_warnings: list[str]) -> None:
    mood_counts: dict[str, int] = {}
    no_mood = 0
    tag_set: set[str] = set()
    activity_counts: dict[str, int] = {}
    titled = 0

    for m in moments:
        mood = m.get("primary_mood_name")
        if mood:
            mood_counts[mood] = mood_counts.get(mood, 0) + 1
        else:
            no_mood += 1
        tag_set.update(m.get("tags", []))
        if m["entry"].get("title"):
            titled += 1
        for ma in m.get("mood_activity", []):
            if ma.get("activity_name"):
                a = ma["activity_name"]
                activity_counts[a] = activity_counts.get(a, 0) + 1

    print(f"\n{'─' * 60}")
    print(f"  DRY RUN — no files written")
    print(f"{'─' * 60}")
    print(f"  Total moments to migrate:  {len(moments)}")
    print(f"  Entries with a title:      {titled}")

    print(f"\n  Mood distribution:")
    for mood, count in sorted(mood_counts.items(), key=lambda x: -x[1]):
        print(f"    {mood:<20} {count}")
    if no_mood:
        print(f"    {'(no mood)':<20} {no_mood}")

    if activity_counts:
        print(f"\n  Activity assignments:")
        for act, count in sorted(activity_counts.items(), key=lambda x: -x[1]):
            print(f"    {act:<20} {count}")

    print(f"\n  Unique plain tags:  {len(tag_set)}")
    for t in sorted(tag_set):
        print(f"    #{t}")

    if all_warnings:
        print(f"\n  ⚠  Warnings ({len(all_warnings)}):")
        for w in all_warnings:
            print(f"    • {w}")

    print(f"\n  First 3 entries (preview):")
    for m in moments[:3]:
        date = m["logged_date_tz"]
        mood = m.get("primary_mood_name", "(none)")
        title = m["entry"].get("title", "")
        activities = [ma["activity_name"] for ma in m.get("mood_activity", []) if ma.get("activity_name")]
        tags = ", ".join(f"#{t}" for t in m.get("tags", [])) or "(none)"
        text = m["entry"].get("content_plain_text", "")[:100].replace("\n", " ").strip()
        print(f"\n    [{date}] mood={mood}  activities={activities or '[]'}  tags={tags}")
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
                        help="Memos JSON export file")
    source.add_argument("--api-url", metavar="URL",
                        help="Memos instance URL (e.g. http://localhost:5230)")

    parser.add_argument("--api-token", metavar="TOKEN",
                        help="Memos access token (required with --api-url)")
    parser.add_argument("--reference-export", "-r", metavar="ZIP", required=True,
                        help="A Journiv export ZIP from your instance (provides UUIDs)")
    parser.add_argument("--output", "-o", metavar="FILE",
                        default="memos_to_journiv_import.zip",
                        help="Output ZIP file (default: memos_to_journiv_import.zip)")
    parser.add_argument("--config", "-c", metavar="FILE",
                        default="config/mood_mapping.yaml",
                        help="Config file (default: config/mood_mapping.yaml)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing any files")
    parser.add_argument("--include-archived", action="store_true",
                        help="Include archived memos (excluded by default)")
    args = parser.parse_args()

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}  (run from repo root)")
        sys.exit(1)
    cfg = load_config(str(config_path))
    print(f"Config: {len(cfg['mood_map'])} mood rules, "
          f"{len(cfg['activity_map'])} activity rules, "
          f"{len(cfg['exclude_tags'])} excluded tags, "
          f"timezone={cfg['user_timezone']}")

    # Load reference export
    ref_path = Path(args.reference_export)
    if not ref_path.exists():
        print(f"ERROR: reference export not found: {ref_path}")
        sys.exit(1)
    ref = load_reference_export(str(ref_path))
    mood_by_name, activity_by_name, journal_id = build_lookup_tables(ref)
    print(f"Reference: {len(mood_by_name)} moods, {len(activity_by_name)} activities, "
          f"journal={ref['journals'][0]['title']!r}")

    # Validate mood map targets exist in the reference
    for tag, mood_name in cfg["mood_map"].items():
        if mood_name not in mood_by_name:
            print(f"WARNING: mood {mood_name!r} (mapped from #{tag}) not found in "
                  f"reference export. Available: {list(mood_by_name.keys())}")

    # Validate activity map targets exist in the reference
    for tag, act_name in cfg["activity_map"].items():
        if act_name not in activity_by_name:
            print(f"WARNING: activity {act_name!r} (mapped from #{tag}) not found in "
                  f"reference export. Available: {list(activity_by_name.keys())}")

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
            print("ERROR: --api-token is required with --api-url")
            sys.exit(1)
        print(f"Fetching memos from API: {args.api_url}")
        raw_memos = list(load_from_api(args.api_url, args.api_token))

    print(f"Loaded {len(raw_memos)} memos")

    # Filter archived
    if not args.include_archived:
        before = len(raw_memos)
        raw_memos = [
            m for m in raw_memos
            if m.get("state", "NORMAL").upper() != "ARCHIVED"
            and m.get("rowStatus", "NORMAL").upper() != "ARCHIVED"
        ]
        if before - len(raw_memos):
            print(f"Skipped {before - len(raw_memos)} archived memos "
                  f"(use --include-archived to include them)")

    # Transform
    all_warnings: list[str] = []
    moments: list[dict] = []
    for memo in raw_memos:
        moment, warnings = transform_memo(memo, cfg, mood_by_name, activity_by_name, journal_id)
        moments.append(moment)
        all_warnings.extend(warnings)

    # Sort chronologically
    moments.sort(key=lambda m: m["logged_at_utc"])

    if args.dry_run:
        print_dry_run_report(moments, all_warnings)
        return

    if all_warnings:
        print(f"\n⚠  {len(all_warnings)} warning(s):")
        for w in all_warnings:
            print(f"  • {w}")

    # Build and write ZIP
    import_data = build_import_data(moments, ref)
    build_import_zip(import_data, args.output)
    size_kb = Path(args.output).stat().st_size // 1024
    print(f"\nWrote {len(moments)} moments to: {args.output}  ({size_kb} KB)")
    print("\nNEXT STEPS:")
    print("  1. Open Journiv → Settings → Import")
    print("  2. Select the ZIP file above")
    print("  3. Wait for the async import job to complete")
    print("  4. Verify entry count and spot-check a few entries for correct")
    print("     date, content, mood, activities, and tags")
    print("  5. If anything looks wrong: docker compose down -v && docker compose up -d")
    print("     then re-import — Memos is untouched throughout")


if __name__ == "__main__":
    main()
