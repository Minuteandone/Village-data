#!/usr/bin/env python3
"""Export all public AI Village chat messages from an inclusive range of days.

Standard library only. GitHub Actions downloads the .txt output as an artifact.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
from pathlib import Path
import random
import re
import shutil
import sys
import time
from urllib import error, parse, request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_BASE = "https://theaidigest.org/village/api"
TALK_TYPES = {"AGENT_TALK", "USER_TALK"}
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}
CONFIG_TYPES = {
    "slug": str, "start_date": str, "end_date": str,
    "retries": int, "retry_delay_seconds": float, "day_retries": int,
    "request_gap_seconds": float, "max_pages": int, "timezone": str,
}


def parse_date(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"Invalid date {value!r}; expected YYYY-MM-DD")
    return dt.date.fromisoformat(value)


def days_between(start, end):
    current = start
    while current <= end:
        yield current.isoformat()
        current += dt.timedelta(days=1)


def timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value
    except ValueError:
        return None


def render_message(name, label, content):
    name = " ".join(str(name).splitlines()).strip() or "Unknown speaker"
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    first = f"{name} ({label}): {lines[0]}"
    return first + ("\n" + "\n".join("    " + line for line in lines[1:])
                    if len(lines) > 1 else "") + "\n"


class PublicApi:
    def __init__(self, retries=5, retry_delay_seconds=2.0,
                 request_gap_seconds=1.25, sleep=time.sleep, clock=time.monotonic):
        self.retries = retries
        self.retry_delay_seconds = retry_delay_seconds
        self.request_gap_seconds = request_gap_seconds
        self.sleep = sleep
        self.clock = clock
        self.last_request = None

    def _backoff(self, attempt):
        return min(120.0, self.retry_delay_seconds * 2**attempt + random.uniform(0, 0.4))

    def _retry_after(self, header, attempt):
        if header:
            try:
                return min(300.0, max(0.0, float(header)))
            except ValueError:
                try:
                    due = email.utils.parsedate_to_datetime(header)
                    return min(300.0, max(0.0, (due - dt.datetime.now(dt.timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        return self._backoff(attempt)

    def get(self, path, **params):
        url = API_BASE + "/" + path.lstrip("/")
        if params:
            url += "?" + parse.urlencode(params)
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                gap = self.request_gap_seconds - (self.clock() - self.last_request)
                if gap > 0:
                    self.sleep(gap)
            self.last_request = self.clock()
            req = request.Request(url, headers={
                "User-Agent": "VillageData-PlainTextExporter/1.0", "Accept": "application/json"})
            try:
                with request.urlopen(req, timeout=45) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Non-object JSON response: {url}")
                if payload.get("error"):
                    raise RuntimeError(f"API error at {url}: {payload['error']}")
                return payload
            except error.HTTPError as exc:
                if exc.code not in RETRY_CODES or attempt == self.retries:
                    raise RuntimeError(f"HTTP {exc.code} at {url}") from exc
                pause = self._retry_after(exc.headers.get("Retry-After"), attempt)
            except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    raise RuntimeError(f"Could not fetch {url}: {exc}") from exc
                pause = self._backoff(attempt)
            print(f"    Retry {attempt + 1}/{self.retries} in {pause:.1f}s: {url}", flush=True)
            self.sleep(pause)
        raise RuntimeError(f"Retry limit exhausted at {url}")


def village_info(api, slug):
    summary = api.get("villages", slug=slug)
    village_id = summary.get("id")
    if not isinstance(village_id, str) or not village_id:
        raise ValueError(f"No public village found for slug {slug!r}")
    if summary.get("slug") and summary["slug"] != slug:
        raise ValueError(f"Requested slug {slug!r}, API returned {summary['slug']!r}")
    details = api.get("villages/" + parse.quote(village_id, safe=""))
    agents = {}
    for agent in details.get("agents", []):
        if isinstance(agent, dict) and agent.get("id"):
            name = agent.get("name") or agent.get("displayName")
            if name:
                agents[str(agent["id"])] = str(name)
    return village_id, agents


def chat_row(event, agents, zone):
    data = event.get("data")
    if not isinstance(data, dict) or data.get("actionType") not in TALK_TYPES:
        return None
    kind = data["actionType"]
    speaker_id = str(data.get("speakerId") or data.get("agentId") or "")
    if kind == "USER_TALK":
        name = data.get("speakerName") or "Human visitor"
    else:
        name = agents.get(speaker_id) or data.get("speakerName") or "Unknown agent"
    when = timestamp(event.get("createdAt") or data.get("createdAt"))
    label = when.astimezone(zone).strftime("%I:%M %p").lstrip("0") if when else "Unknown time"
    index = event.get("eventIndex")
    index = index if isinstance(index, int) else 10**18
    order_time = when.astimezone(dt.timezone.utc).timestamp() if when else float("inf")
    return (order_time, index, str(event.get("id") or ""),
            render_message(name, label, data.get("content")))


def export_day(api, village_id, day, agents, zone, max_pages, destination):
    """Fetch every event page before writing a successful day's output."""
    messages = []
    seen = set()
    inspected = 0
    duplicate_count = 0
    pages = 0
    for page in range(1, max_pages + 1):
        payload = api.get("events", villageId=village_id, date=day, page=page)
        events = payload.get("events")
        has_more = payload.get("hasMore")
        if not isinstance(events, list) or not isinstance(has_more, bool):
            raise RuntimeError(f"{day} page {page}: missing events or hasMore field")
        if payload.get("windowDate") and payload["windowDate"] != day:
            raise RuntimeError(f"{day} page {page}: wrong windowDate {payload['windowDate']}")
        if has_more and not events:
            raise RuntimeError(f"{day} page {page}: empty page while hasMore=true")
        pages = page
        inspected += len(events)
        for position, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            row = chat_row(event, agents, zone)
            if row is None:
                continue
            key = (("id", str(event["id"])) if event.get("id") else
                   ("index", event["eventIndex"]) if event.get("eventIndex") is not None
                   else ("position", page, position))
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            messages.append(row)
        if page % 25 == 0:
            print(f"    {day}: {page} pages, {len(messages)} messages", flush=True)
        if not has_more:
            break
    else:
        raise RuntimeError(f"{day}: reached max_pages={max_pages} with hasMore=true")
    messages.sort(key=lambda row: (row[0], row[1], row[2]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            for row in messages:
                output.write(row[3])
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {"date": day, "status": "complete", "messages": len(messages),
            "event_pages": pages, "events_inspected": inspected,
            "duplicates_discarded": duplicate_count, "file": destination.name}


def write_manifest(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_export(settings, output_dir, api=None):
    slug = settings["slug"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Village slug must use lowercase letters, digits and hyphens")
    start = parse_date(settings["start_date"])
    end = parse_date(settings["end_date"])
    if start > end:
        raise ValueError("Start date must not be after end date")
    if settings["retries"] < 0 or settings["day_retries"] < 0:
        raise ValueError("Retry counts cannot be negative")
    if settings["retry_delay_seconds"] < 0 or settings["request_gap_seconds"] < 0:
        raise ValueError("Request gap and retry delay cannot be negative")
    if settings["max_pages"] < 1:
        raise ValueError("max_pages must be at least 1")
    try:
        zone = ZoneInfo(settings["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone {settings['timezone']!r}") from exc
    api = api or PublicApi(settings["retries"], settings["retry_delay_seconds"],
                           settings["request_gap_seconds"])
    village_id, agents = village_info(api, slug)
    directory = output_dir / slug
    directory.mkdir(parents=True, exist_ok=True)
    chosen_days = list(days_between(start, end))
    report = {"slug": slug, "village_id": village_id, "start_date": start.isoformat(),
              "end_date": end.isoformat(), "timezone": settings["timezone"],
              "source": API_BASE, "complete": False, "total_messages": 0,
              "completed_days": 0, "failed_days": [], "days": []}
    manifest = directory / "manifest.json"
    print(f"Village {slug!r}, {len(chosen_days)} days, ID {village_id}", flush=True)
    for number, day in enumerate(chosen_days, 1):
        destination = directory / (day + ".txt")
        print(f"[{number}/{len(chosen_days)}] {day}", flush=True)
        for attempt in range(settings["day_retries"] + 1):
            try:
                result = export_day(api, village_id, day, agents, zone,
                                    settings["max_pages"], destination)
                report["days"].append(result)
                report["total_messages"] += result["messages"]
                report["completed_days"] += 1
                print(f"    Complete: {result['messages']} messages, "
                      f"{result['event_pages']} pages", flush=True)
                break
            except Exception as exc:
                destination.unlink(missing_ok=True)
                if attempt == settings["day_retries"]:
                    report["failed_days"].append(day)
                    report["days"].append({"date": day, "status": "failed", "error": str(exc)})
                    print(f"    FAILED {day}: {exc}", file=sys.stderr, flush=True)
                else:
                    pause = min(120.0, settings["retry_delay_seconds"] * (2**attempt))
                    print(f"    Whole-day retry {attempt + 1} in {pause:.1f}s", flush=True)
                    api.sleep(pause)
        write_manifest(manifest, report)
    combined = directory / f"messages-{start.isoformat()}-to-{end.isoformat()}.txt"
    temporary = combined.with_suffix(".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as out:
            for day in report["days"]:
                if day["status"] == "complete":
                    with (directory / day["file"]).open("r", encoding="utf-8") as part:
                        shutil.copyfileobj(part, out)
        temporary.replace(combined)
    finally:
        temporary.unlink(missing_ok=True)
    report["complete"] = not report["failed_days"]
    report["combined_file"] = combined.name
    report["generated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_manifest(manifest, report)
    print(f"RESULT: {report['total_messages']} messages; "
          f"{report['completed_days']}/{len(chosen_days)} days completed; "
          f"complete={report['complete']}", flush=True)
    return report


def load_settings(args, environ=None):
    environment = os.environ if environ is None else environ
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("config.json must contain an object")
    settings = {}
    for key, kind in CONFIG_TYPES.items():
        value = getattr(args, key)
        if value is None:
            value = environment.get("EXPORT_" + key.upper()) or config.get(key)
        if value is None or value == "":
            raise ValueError(f"Missing setting {key!r}")
        try:
            if kind is int:
                if isinstance(value, float) and not value.is_integer():
                    raise ValueError()
                value = int(value)
            elif kind is float:
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid setting {key}: {value!r}") from exc
        settings[key] = value
    return settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output-dir", default="output")
    for key in CONFIG_TYPES:
        parser.add_argument("--" + key.replace("_", "-"), dest=key, default=None)
    args = parser.parse_args(argv)
    try:
        report = run_export(load_settings(args), Path(args.output_dir))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    if not report["complete"]:
        print("Some days failed. Check manifest.json in the uploaded artifact.",
              file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
