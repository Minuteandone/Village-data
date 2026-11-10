"""Offline contract tests for the AI Village public-message exporter."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_messages as exp


class ExampleApi:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []
        self.sleep = lambda seconds: None

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "villages":
            return {"id": "example-id", "slug": params["slug"]}
        if path == "villages/example-id":
            return {"agents": [{"id": "agent-1", "name": "Claude Fable 5.2"}]}
        if path == "events":
            value = self.responses.get((params["date"], params["page"]))
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value if value is not None else
                                 {"events": [], "hasMore": False, "windowDate": params["date"]})
        raise AssertionError("Unexpected API path: " + path)


def record(eid, when, kind, message, **kwargs):
    return {"id": eid, "eventIndex": int(eid[1:]), "createdAt": when,
            "data": {"actionType": kind, "content": message, **kwargs}}


SETTINGS = {
    "slug": "actual-launch-1", "start_date": "2026-11-09", "end_date": "2026-11-10",
    "retries": 2, "retry_delay_seconds": 0, "day_retries": 1,
    "request_gap_seconds": 0, "max_pages": 10, "timezone": "America/Los_Angeles",
}


class FormattingTests(unittest.TestCase):
    def test_multiline(self):
        self.assertEqual(exp.render_message("Agent", "9:01 AM", "Hi\nthere\n"),
                         "Agent (9:01 AM): Hi\n    there\n    \n")

    def test_inclusive_dates(self):
        start, end = exp.parse_date("2026-11-09"), exp.parse_date("2026-11-10")
        self.assertEqual(list(exp.days_between(start, end)), ["2026-11-09", "2026-11-10"])

    def test_bad_date(self):
        for value in ("2026-2-01", "2026-02-30", "tomorrow"):
            with self.assertRaises(ValueError):
                exp.parse_date(value)


class ArchiveTests(unittest.TestCase):
    def test_full_pagination_order_duplicates(self):
        newer = record("e2", "2026-11-09T19:01:00Z", "USER_TALK", "Hey!\nSecond line",
                       speakerName="Starpetter")
        older = record("e1", "2026-11-09T19:00:00Z", "AGENT_TALK", "Hello world!",
                       speakerId="agent-1")
        api = ExampleApi({
            ("2026-11-09", 1): {"events": [newer], "hasMore": True, "windowDate": "2026-11-09"},
            ("2026-11-09", 2): {"events": [older, newer], "hasMore": False,
                                   "windowDate": "2026-11-09"},
        })
        with tempfile.TemporaryDirectory() as temp:
            result = exp.run_export(SETTINGS, Path(temp), api)
            folder = Path(temp) / "actual-launch-1"
            daily = (folder / "2026-11-09.txt").read_text(encoding="utf-8")
            combined = (folder / "messages-2026-11-09-to-2026-11-10.txt").read_text()
            manifest = json.loads((folder / "manifest.json").read_text())
            self.assertEqual(daily,
                             "Claude Fable 5.2 (11:00 AM): Hello world!\n"
                             "Starpetter (11:01 AM): Hey!\n    Second line\n")
            self.assertEqual(combined, daily)
            self.assertTrue(result["complete"])
            self.assertEqual(result["total_messages"], 2)
            self.assertEqual(manifest["days"][0]["event_pages"], 2)
            self.assertEqual(manifest["days"][0]["duplicates_discarded"], 1)
            self.assertEqual((folder / "2026-11-10.txt").read_text(), "")

    def test_failed_page_not_silently_complete(self):
        api = ExampleApi({
            ("2026-11-09", 1): {"events": [record("e1", "2026-11-09T19:00:00Z",
                                                   "AGENT_TALK", "Hi", speakerId="agent-1")],
                                   "hasMore": True},
            ("2026-11-09", 2): RuntimeError("network failed")
        })
        with tempfile.TemporaryDirectory() as temp:
            result = exp.run_export(SETTINGS, Path(temp), api)
            folder = Path(temp) / "actual-launch-1"
            self.assertFalse(result["complete"])
            self.assertEqual(result["failed_days"], ["2026-11-09"])
            self.assertFalse((folder / "2026-11-09.txt").exists())
            self.assertTrue((folder / "2026-11-10.txt").exists())
            self.assertEqual((folder / result["combined_file"]).read_text(), "")

    def test_empty_page_with_has_more_is_error(self):
        api = ExampleApi({("2026-11-09", 1): {"events": [], "hasMore": True}})
        with tempfile.TemporaryDirectory() as temp:
            setting = dict(SETTINGS, end_date="2026-11-09", day_retries=0)
            result = exp.run_export(setting, Path(temp), api)
            self.assertFalse(result["complete"])
            self.assertIn("hasMore=true", result["days"][0]["error"])

    def test_config_env_override(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / "config.json"
            file.write_text(json.dumps(SETTINGS))
            args = exp.argparse.Namespace(config=str(file),
                                           **{key: None for key in exp.CONFIG_TYPES})
            result = exp.load_settings(args, {"EXPORT_SLUG": "open-chat", "EXPORT_RETRIES": "4"})
            self.assertEqual(result["slug"], "open-chat")
            self.assertEqual(result["retries"], 4)

    def test_http_retry(self):
        from io import BytesIO
        from urllib.error import HTTPError
        failure = HTTPError("https://example.org", 429, "Too Many Requests",
                            {"Retry-After": "1"}, None)
        delays = []
        api = exp.PublicApi(retries=1, retry_delay_seconds=0, request_gap_seconds=0,
                            sleep=delays.append)
        class FakeResponse:
            def __enter__(self):
                return BytesIO(b'{"events":[],"hasMore":false}')
            def __exit__(self, *unused):
                return False
        with patch.object(exp.request, "urlopen", side_effect=[failure, FakeResponse()]) as send:
            response = api.get("events", date="2026-11-09")
        self.assertEqual(response["events"], [])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(delays, [1.0])

    def test_invalid_slug(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "slug"):
                exp.run_export(dict(SETTINGS, slug="../private"), Path(temp), ExampleApi())


if __name__ == "__main__":
    unittest.main()
