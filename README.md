# memos-to-Journiv-import

Migrates journal entries from [Memos](https://github.com/usememos/memos) to [Journiv](https://github.com/journiv/journiv-app), including hashtag → mood/tag conversion.

**Data safety:** this toolset is strictly read-only with respect to Memos. Nothing in your Memos database is ever modified or deleted. Journiv is the throwaway side — if an import goes wrong, wipe Journiv and try again.

---

## Overview

```
memos_export.json  →  scripts/migrate.py  →  memos_to_journiv_import.zip  →  Journiv import
       ↑                      ↑
  (your data)         config/mood_mapping.yaml
```

---

## Prerequisites

- Python 3.11+
- Docker + Docker Compose (for Journiv deployment)
- Your Memos instance running (it stays running throughout — we only read from it)

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Step 1 — Export your Memos data

You need a JSON export of your Memos entries. Two options:

### Option A — REST API (recommended)

Get your access token from **Memos → Settings → Account → Access Tokens**, then:

```bash
curl -X GET "http://localhost:5230/api/v1/memos?pageSize=1000" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -o memos_export.json
```

If you have more than 1000 memos, paginate:

```bash
# The response includes a "nextPageToken" field — use it to get the next page
curl -X GET "http://localhost:5230/api/v1/memos?pageSize=1000&pageToken=NEXT_PAGE_TOKEN" \
  -H "Authorization: Bearer YOUR_TOKEN" >> memos_export.json
```

Or use the built-in paginating mode of the migration script:

```bash
python3 scripts/migrate.py --api-url http://localhost:5230 --api-token YOUR_TOKEN --dry-run
```

### Option B — Built-in UI export

**Memos → Settings → Export → JSON** → save as `memos_export.json` in the repo root.

---

## Step 2 — Deploy Journiv

```bash
cd journiv/
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Value |
|---|---|
| `SECRET_KEY` | Run: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DOMAIN_NAME` | Your server's LAN IP, e.g. `192.168.1.100` — **not** `localhost` |
| `APP_PORT` | `8000` (Memos uses 5230, so this is free) |

Then start Journiv:

```bash
docker compose up -d
```

Access Journiv at `http://YOUR_IP:8000` and create your account.

> **Troubleshooting:** if the app doesn't load, the most common cause is `DOMAIN_NAME` set to `localhost`. Set it to the actual LAN IP of the server. Journiv uses this for CORS/same-origin SPA mode.

---

## Step 3 — Discover Journiv's export format (important)

Because Journiv's import format isn't publicly documented, this step reverse-engineers it from a real export so the migration script targets the correct schema.

1. In Journiv, create 2–3 test entries with different moods
2. Go to **Settings → Export** → download the ZIP
3. Run:

```bash
python3 scripts/inspect_journiv_export.py journiv_sample_export.zip
```

This prints the exact field names and writes `journiv_entry_schema.json`.

If the field names differ from what `scripts/migrate.py` produces, adjust the `transform_memo()` function in `migrate.py` (the fields are clearly labelled).

---

## Step 4 — Configure mood mapping

Edit `config/mood_mapping.yaml` to map your Memos hashtags to Journiv moods:

```yaml
mood_map:
  happy: "Rad"     # #happy in Memos → mood "Rad" in Journiv
  sad: "Bad"
  # ...

strip_mood_hashtags_from_content: true  # remove matched #tags from entry body
```

**The mood names must exactly match your Journiv instance.** Check **Journiv → Settings → Moods** and update `mood_map` values to match. Journiv's default moods are `Rad`, `Good`, `Meh`, `Bad`, `Awful`.

Hashtags not in the `mood_map` become Journiv tags (not moods).

---

## Step 5 — Run the migration

### Dry run first

```bash
python3 scripts/migrate.py --input memos_export.json --dry-run
```

Review the output:
- Total entry count
- Mood distribution (how many entries get each mood)
- Tag list
- Preview of the first few entries

### Full migration

```bash
python3 scripts/migrate.py --input memos_export.json
```

This writes `memos_to_journiv_import.zip`.

---

## Step 6 — Import into Journiv

1. Open Journiv → **Settings → Import**
2. Select `memos_to_journiv_import.zip`
3. Wait for the import job to complete (Journiv processes it asynchronously)
4. Verify:
   - Entry count matches the dry-run output
   - Spot-check 5–10 entries for correct date, content, mood, and tags

If something looks wrong, wipe Journiv and try again:

```bash
cd journiv/
docker compose down -v   # removes all data volumes
docker compose up -d     # fresh start
```

---

## Command reference

```bash
# Dry run with a local export file
python3 scripts/migrate.py --input memos_export.json --dry-run

# Full migration
python3 scripts/migrate.py --input memos_export.json

# Custom output path
python3 scripts/migrate.py --input memos_export.json --output my_import.zip

# Live API mode (auto-paginates)
python3 scripts/migrate.py --api-url http://localhost:5230 --api-token YOUR_TOKEN

# Include archived memos (excluded by default)
python3 scripts/migrate.py --input memos_export.json --include-archived

# Custom config file
python3 scripts/migrate.py --input memos_export.json --config config/mood_mapping.yaml
```

---

## What gets migrated

| Memos field | Journiv field | Notes |
|---|---|---|
| `content` (Markdown) | `content` (Quill Delta) | Formatting converted automatically |
| `createTime` | `date` | Full timestamp preserved |
| `updateTime` | `updated_at` | Full timestamp preserved |
| `tags` / `#hashtags` | `mood` + `tags` | Via `config/mood_mapping.yaml` |
| `pinned` | `starred` | Mapped directly |
| `visibility` | — | Not migrated (Journiv is private-only) |
| Attachments/images | — | Not migrated (text-only migration) |

---

## File structure

```
├── journiv/
│   ├── docker-compose.yml      Journiv SQLite deployment
│   └── .env.example            Environment variable template
├── config/
│   └── mood_mapping.yaml       Hashtag → mood mapping table
├── scripts/
│   ├── inspect_journiv_export.py   Discover Journiv's import format
│   ├── migrate.py                  Main migration script
│   └── lib/
│       ├── quill_delta.py          Markdown → Quill Delta converter
│       └── memos_reader.py         Memos API / JSON file reader
├── requirements.txt
└── README.md
```
