"""Run with:  python -m unittest -v
Tests use a fake Gemini client, so no API key or internet is needed."""
import io
import json
import sys
import types as pytypes
import unittest

import pandas as pd

# If google-genai is not installed, provide a tiny stand-in so the logic can still be tested.
try:
    import google.genai  # noqa: F401
except ImportError:
    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    types_mod = pytypes.ModuleType("google.genai.types")

    class _Part:
        @staticmethod
        def from_bytes(data, mime_type):
            return {"data": data, "mime_type": mime_type}

    class _Config:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    types_mod.Part, types_mod.GenerateContentConfig = _Part, _Config
    genai_mod.types = types_mod
    google_mod.genai = genai_mod
    sys.modules.update({"google": google_mod, "google.genai": genai_mod, "google.genai.types": types_mod})

import planner  # noqa: E402

SETTINGS = {
    "window_start": "16:00",
    "window_end": "21:00",
    "days": ["Monday", "Tuesday"],
    "session_minutes": 50,
    "break_minutes": 10,
    "subjects": "Maths, Physics",
    "goals": "",
    "current_planner": "",
}

GOOD = {
    "summary": "Plan.",
    "busy_blocks": [{"day": "Monday", "start": "09:00", "end": "15:00", "activity": "Classes"}],
    "timetable": [
        {"day": "Tuesday", "start": "16:00", "end": "16:50", "subject": "Physics", "activity_type": "study", "notes": ""},
        {"day": "monday", "start": "4:00 PM", "end": "4:50 PM", "subject": "Maths", "activity_type": "practice", "notes": "x"},
        {"day": "Monday", "start": "16:50", "end": "17:00", "subject": "Break", "activity_type": "break", "notes": ""},
    ],
    "improvements": [
        {"title": "Low one", "detail": "d", "priority": "low"},
        {"title": "High one", "detail": "d", "priority": "high"},
    ],
    "warnings": [],
}


class FakeClient:
    def __init__(self, *texts):
        self.texts, self.calls = list(texts), 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls += 1
        self.last_contents = contents
        return pytypes.SimpleNamespace(text=self.texts.pop(0))


class TimeTests(unittest.TestCase):
    def test_to_minutes(self):
        self.assertEqual(planner.to_minutes("16:30"), 990)
        self.assertEqual(planner.to_minutes("4:30 PM"), 990)
        self.assertEqual(planner.to_minutes("12:00 AM"), 0)
        self.assertEqual(planner.to_minutes("12:15 PM"), 735)
        self.assertEqual(planner.to_minutes("9"), 540)
        self.assertIsNone(planner.to_minutes("25:00"))
        self.assertIsNone(planner.to_minutes("abc"))

    def test_normalize_day(self):
        self.assertEqual(planner.normalize_day("mon"), "Monday")
        self.assertEqual(planner.normalize_day("THURS"), "Thursday")
        self.assertEqual(planner.normalize_day("Sunday"), "Sunday")
        self.assertIsNone(planner.normalize_day("someday"))
        self.assertIsNone(planner.normalize_day(""))


class LoadScheduleTests(unittest.TestCase):
    def test_csv(self):
        out = planner.load_schedule("s.csv", b"Day,Time,Class\nMon,9-10,Maths\n")
        self.assertEqual(out["kind"], "text")
        self.assertIn("Maths", out["text"])

    def test_xlsx(self):
        buf = io.BytesIO()
        pd.DataFrame([["Mon", "9-10", "Maths"]]).to_excel(buf, index=False, header=False)
        out = planner.load_schedule("s.xlsx", buf.getvalue())
        self.assertIn("Maths", out["text"])

    def test_txt_and_binary(self):
        self.assertEqual(planner.load_schedule("s.txt", b"Mon 9-10 Maths")["kind"], "text")
        img = planner.load_schedule("s.JPG", b"\xff\xd8\xff")
        self.assertEqual((img["kind"], img["mime"]), ("binary", "image/jpeg"))
        self.assertEqual(planner.load_schedule("s.pdf", b"%PDF-1.4")["mime"], "application/pdf")

    def test_errors(self):
        for name, data in [("s.csv", b""), ("s.exe", b"x"), ("s.txt", b"   \n"), ("noext", b"x")]:
            with self.assertRaises(planner.ScheduleError, msg=name):
                planner.load_schedule(name, data)
        with self.assertRaises(planner.ScheduleError):
            planner.load_schedule("big.txt", b"x" * (planner.MAX_FILE_BYTES + 1))


class ParseTests(unittest.TestCase):
    def test_fenced_and_noisy_json(self):
        self.assertEqual(planner.parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(planner.parse_json('Sure! {"a": 1} hope that helps'), {"a": 1})

    def test_invalid(self):
        for bad in ["", "no json", "[1, 2]", "{broken"]:
            with self.assertRaises(planner.PlanError, msg=bad):
                planner.parse_json(bad)


class NormalizeTests(unittest.TestCase):
    def test_normalize_sorts_and_cleans(self):
        plan = planner.normalize_plan(GOOD)
        self.assertEqual([r["day"] for r in plan["timetable"]], ["Monday", "Monday", "Tuesday"])
        self.assertEqual(plan["timetable"][0]["start"], "16:00")  # "4:00 PM" converted
        self.assertEqual(plan["improvements"][0]["priority"], "high")

    def test_bad_rows_skipped_with_warning(self):
        raw = {"timetable": [{"day": "Mon", "start": "10:00", "end": "09:00"}, "junk", {"day": "Mon", "start": "10:00", "end": "11:00"}]}
        plan = planner.normalize_plan(raw)
        self.assertEqual(len(plan["timetable"]), 1)
        self.assertEqual(plan["timetable"][0]["subject"], "Study")
        self.assertTrue(any("2 timetable row" in w for w in plan["warnings"]))

    def test_missing_keys(self):
        plan = planner.normalize_plan({})
        self.assertEqual(plan["timetable"], [])
        self.assertEqual(plan["improvements"], [])


class CheckTests(unittest.TestCase):
    def test_detects_problems(self):
        plan = planner.normalize_plan(
            {
                "busy_blocks": [{"day": "Monday", "start": "17:00", "end": "18:00", "activity": "Gym"}],
                "timetable": [
                    {"day": "Monday", "start": "15:00", "end": "16:30", "subject": "A"},  # outside window
                    {"day": "Monday", "start": "16:00", "end": "17:30", "subject": "B"},  # overlaps A and Gym
                    {"day": "Friday", "start": "16:00", "end": "17:00", "subject": "C"},  # unselected day
                ],
            }
        )
        text = " | ".join(planner.check_plan(plan, "16:00", "21:00", ["Monday", "Tuesday"]))
        self.assertIn("outside your free-time window", text)
        self.assertIn("overlaps 'Gym'", text)
        self.assertIn("'A' and 'B' overlap", text)
        self.assertIn("day was not selected", text)

    def test_clean_plan_has_no_issues(self):
        plan = planner.normalize_plan(GOOD)
        self.assertEqual(planner.check_plan(plan, "16:00", "21:00", ["Monday", "Tuesday"]), [])

    def test_weekly_hours_excludes_breaks(self):
        hours = planner.weekly_hours(planner.normalize_plan(GOOD))
        self.assertEqual(hours, {"Physics": 0.83, "Maths": 0.83})


class GenerateTests(unittest.TestCase):
    def test_success_text_schedule(self):
        client = FakeClient(json.dumps(GOOD))
        plan = planner.generate_plan({"kind": "text", "text": "Mon classes"}, SETTINGS, "k", client=client)
        self.assertEqual(len(plan["timetable"]), 3)
        self.assertIn("weekly_hours", plan)
        self.assertEqual(len(client.last_contents), 1)
        self.assertIn("Mon classes", client.last_contents[0])

    def test_binary_schedule_is_attached(self):
        client = FakeClient(json.dumps(GOOD))
        planner.generate_plan({"kind": "binary", "mime": "image/png", "data": b"x"}, SETTINGS, "k", client=client)
        self.assertEqual(len(client.last_contents), 2)

    def test_retry_then_success(self):
        client = FakeClient("garbage", json.dumps(GOOD))
        plan = planner.generate_plan({"kind": "text", "text": "x"}, SETTINGS, "k", client=client)
        self.assertEqual(client.calls, 2)
        self.assertTrue(plan["timetable"])

    def test_gives_up_after_retries(self):
        client = FakeClient("garbage", "more garbage")
        with self.assertRaises(planner.PlanError):
            planner.generate_plan({"kind": "text", "text": "x"}, SETTINGS, "k", client=client)

    def test_missing_key(self):
        with self.assertRaises(planner.PlanError):
            planner.generate_plan({"kind": "text", "text": "x"}, SETTINGS, "")

    def test_prompt_contains_constraints(self):
        prompt = planner.build_prompt(SETTINGS, None)
        for expected in ["16:00 to 21:00", "Monday, Tuesday", "50 minutes", "Maths, Physics", "attached file"]:
            self.assertIn(expected, prompt)


ERR_503 = "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand.', 'status': 'UNAVAILABLE'}}"


class ScriptedClient:
    """Each item is either an Exception to raise or a response text. Records (model, ...) per call."""

    def __init__(self, *script):
        self.script, self.models_called = list(script), []
        self.models = self

    def generate_content(self, model, contents, config):
        self.models_called.append(model)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return pytypes.SimpleNamespace(text=item)


class ResilienceTests(unittest.TestCase):
    def run_plan(self, client, **kw):
        delays = []
        plan = planner.generate_plan(
            {"kind": "text", "text": "x"}, SETTINGS, "k", client=client, sleep=delays.append, **kw
        )
        return plan, delays

    def test_503_is_retried_with_backoff_then_succeeds(self):
        client = ScriptedClient(RuntimeError(ERR_503), RuntimeError(ERR_503), json.dumps(GOOD))
        plan, delays = self.run_plan(client)
        self.assertEqual(delays, [2.0, 4.0])
        self.assertEqual(client.models_called, ["gemini-flash-latest"] * 3)
        self.assertEqual(plan["model_used"], "gemini-flash-latest")

    def test_falls_back_to_backup_model_when_main_stays_busy(self):
        client = ScriptedClient(*[RuntimeError(ERR_503)] * 4, json.dumps(GOOD))
        plan, delays = self.run_plan(client)
        self.assertEqual(delays, [2.0, 4.0, 8.0])
        self.assertEqual(client.models_called[-1], "gemini-flash-lite-latest")
        self.assertEqual(plan["model_used"], "gemini-flash-lite-latest")

    def test_quota_error_goes_straight_to_backup_without_waiting(self):
        client = ScriptedClient(RuntimeError("429 RESOURCE_EXHAUSTED"), json.dumps(GOOD))
        plan, delays = self.run_plan(client)
        self.assertEqual(delays, [])
        self.assertEqual(plan["model_used"], "gemini-flash-lite-latest")

    def test_bad_key_or_model_fails_immediately(self):
        client = ScriptedClient(RuntimeError("400 INVALID_ARGUMENT. API key not valid"))
        with self.assertRaises(RuntimeError):
            self.run_plan(client)
        self.assertEqual(len(client.models_called), 1)

    def test_everything_busy_raises_original_error(self):
        client = ScriptedClient(*[RuntimeError(ERR_503)] * 8)
        with self.assertRaises(RuntimeError) as ctx:
            self.run_plan(client)
        self.assertIn("503", str(ctx.exception))
        self.assertEqual(len(client.models_called), 8)  # 4 tries x 2 models

    def test_broken_backup_model_does_not_hide_main_error(self):
        client = ScriptedClient(*[RuntimeError(ERR_503)] * 4, RuntimeError("404 NOT_FOUND model"))
        with self.assertRaises(RuntimeError) as ctx:
            self.run_plan(client)
        self.assertIn("503", str(ctx.exception))

    def test_no_backup_when_disabled(self):
        client = ScriptedClient(*[RuntimeError(ERR_503)] * 4)
        with self.assertRaises(RuntimeError):
            self.run_plan(client, fallback_model="")
        self.assertEqual(set(client.models_called), {"gemini-flash-latest"})


if __name__ == "__main__":
    unittest.main()
