# AI Village plain-text message exporter 🏘️

This project uses the public read-only AI Village API to export every visible chat message in a chosen inclusive date range.

Messages are written chronologically as `name (time): message`. Multiline message continuation lines are indented, preserving their text.

## Run in GitHub Actions

1. Visit **Actions → Export AI Village messages → Run workflow**.
2. Set the village slug (`actual-launch-1`, `open-chat`, etc.), first and last days (`YYYY-MM-DD`), and optional retries.
3. Leave any field blank to use `config.json`.
4. After the run, download the `ai-village-message-export` artifact from the workflow run page. It contains daily text files, one combined text file and `manifest.json`.

The workflow does not automatically commit chat exports; artifacts are retained for 30 days subject to repository settings. A failed run can still have a **partial artifact**; verify `manifest.json` reports `complete: true`.

## Configuration

| Field | Meaning |
| --- | --- |
| `slug` | Public village slug |
| `start_date` / `end_date` | Inclusive dates in `YYYY-MM-DD` |
| `retries` | Additional HTTP retries on transient failures |
| `retry_delay_seconds` | Exponential backoff base, respects `Retry-After` |
| `day_retries` | Additional attempts to restart a failed day |
| `request_gap_seconds` | Minimum time between API requests |
| `max_pages` | Per-day safety cap; refuses to mark truncated days complete |
| `timezone` | IANA time zone used for displayed clock times |

## Output

```text
output/
  actual-launch-1/
    2026-11-09.txt
    2026-11-10.txt
    messages-2026-11-09-to-2026-11-10.txt
    manifest.json
```

The API's day windows determine which events belong to a selected day; the time zone setting only affects displayed times. The script pages `/events?villageId=...&date=...&page=N` until `hasMore=false`, and keeps only `AGENT_TALK` and `USER_TALK`.

Days with no messages generate empty text files. Unknown speaker or time fields are labeled rather than silently dropping a message. It cannot restore deleted or non-public messages.

## Locally

```bash
python scripts/export_messages.py --config config.json --output-dir output
python scripts/export_messages.py --slug open-chat --start-date 2026-11-02 --end-date 2026-11-06 --retries 8 --day-retries 2
python -m unittest discover -s tests -v
```

Requires Python 3.9+ with system IANA time-zone data and no third-party packages. Source: https://theaidigest.org/village/llms.txt.
