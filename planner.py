"""Core logic for the Study Planner app.

This module has no Streamlit imports, so it can be tested without a UI.
It handles: reading the uploaded schedule, building the prompt, calling
Gemini, and cleaning / validating the JSON the model returns.
"""
from __future__ import annotations

import io
import json
import re
import time
from typing import Any

# "gemini-flash-latest" is Google's alias for the newest Flash model, so the app keeps working
# when old model names are retired. You can pin a specific one (e.g. gemini-3.8-flash) in the sidebar.
DEFAULT_MODEL = "gemini-flash-latest"
# Used automatically if the main model stays overloaded (503) or rate-limited (429).
DEFAULT_FALLBACK_MODEL = "gemini-flash-lite-latest"

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_ALIASES = {d[:3].lower(): d for d in DAYS}
_DAY_ALIASES.update({"tues": "Tuesday", "thur": "Thursday", "thurs": "Thursday"})

IMAGE_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
SUPPORTED_TYPES = ["png", "jpg", "jpeg", "webp", "pdf", "csv", "xlsx", "txt", "md"]

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_TEXT_CHARS = 30_000

ACTIVITY_TYPES = {"study", "revision", "practice", "break", "rest"}
NON_STUDY = {"break", "rest"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class ScheduleError(ValueError):
    """The uploaded file could not be used."""


class PlanError(RuntimeError):
    """The AI response could not be turned into a plan."""


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def to_minutes(value: Any) -> int | None:
    """'16:30', '4:30 PM', '16' -> minutes since midnight. None if invalid."""
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([AaPp][Mm])?\s*$", str(value))
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        ampm = ampm.upper()
        if not 1 <= hour <= 12:
            return None
        if ampm == "PM" and hour < 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def from_minutes(total: int) -> str:
    return f"{total // 60:02d}:{total % 60:02d}"


def normalize_day(value: Any) -> str | None:
    text = str(value).strip().lower().rstrip(".")
    if not text:
        return None
    for day in DAYS:
        if text == day.lower():
            return day
    return _DAY_ALIASES.get(text[:4]) or _DAY_ALIASES.get(text[:3])


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


# --------------------------------------------------------------------------
# Reading the uploaded schedule
# --------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _csv_to_text(data: bytes) -> str:
    import pandas as pd

    for enc in ("utf-8-sig", "latin-1"):
        try:
            df = pd.read_csv(io.BytesIO(data), encoding=enc, dtype=str)
            return df.fillna("").to_csv(index=False)
        except UnicodeDecodeError:
            continue
        except pd.errors.EmptyDataError:
            raise ScheduleError("The CSV file is empty.")
        except Exception:
            break
    return _decode(data)  # odd CSV layout: let the AI read the raw text


def _xlsx_to_text(data: bytes) -> str:
    import pandas as pd

    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    except Exception as exc:
        raise ScheduleError(f"Could not read the Excel file: {exc}") from exc
    parts = []
    for name, df in sheets.items():
        df = df.fillna("")
        if df.empty:
            continue
        parts.append(f"Sheet: {name}\n{df.to_csv(index=False, header=False)}")
    return "\n".join(parts)


def load_schedule(filename: str, data: bytes) -> dict:
    """Return {'kind': 'text', 'text': ...} or {'kind': 'binary', 'mime': ..., 'data': ...}."""
    if not data:
        raise ScheduleError("The uploaded file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise ScheduleError(f"File is too large (max {MAX_FILE_BYTES // (1024 * 1024)} MB).")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in IMAGE_MIME:
        return {"kind": "binary", "mime": IMAGE_MIME[ext], "data": data, "name": filename}
    if ext == "pdf":
        return {"kind": "binary", "mime": "application/pdf", "data": data, "name": filename}

    if ext == "csv":
        text = _csv_to_text(data)
    elif ext == "xlsx":
        text = _xlsx_to_text(data)
    elif ext in ("txt", "md"):
        text = _decode(data)
    else:
        raise ScheduleError(
            f"Unsupported file type '.{ext}'. Please upload one of: "
            + ", ".join(SUPPORTED_TYPES)
        )

    text = text.strip()
    if not text:
        raise ScheduleError("No readable content found in the file.")
    return {"kind": "text", "text": text[:MAX_TEXT_CHARS], "name": filename}


# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------
SYSTEM_INSTRUCTION = """You are an expert academic coach and timetable planner for students.
You receive a student's existing schedule (classes, work, commitments) and must build a realistic
weekly study timetable ONLY inside the free time window the student gives you, never overlapping
their existing commitments. Use evidence-based study habits: spaced repetition, active recall,
interleaving subjects, short breaks, harder subjects when energy is high, and a weekly review.

Respond with ONLY a JSON object (no markdown, no commentary) with exactly this shape:
{
  "summary": "2-3 sentence overview of the plan and its logic",
  "busy_blocks": [{"day": "Monday", "start": "09:00", "end": "11:00", "activity": "Maths lecture"}],
  "timetable": [
    {"day": "Monday", "start": "16:00", "end": "16:50", "subject": "Maths",
     "activity_type": "study", "notes": "Practice past-paper questions"}
  ],
  "improvements": [
    {"title": "short title", "detail": "specific, actionable advice", "priority": "high"}
  ],
  "warnings": ["anything the student should know, e.g. too little free time"]
}

Rules:
- day: full English weekday name. start/end: 24-hour "HH:MM".
- activity_type is one of: study, revision, practice, break, rest.
- priority is one of: high, medium, low.
- Include short breaks as timetable rows with activity_type "break".
- Every timetable row must lie inside the free-time window and must not overlap busy_blocks.
- busy_blocks lists the commitments you detected in the uploaded schedule (empty list if none).
- Give 4-8 improvements, specific to THIS student's schedule and goals.
- If the schedule file is unreadable or has no usable information, return empty lists and explain in warnings."""


def build_prompt(settings: dict, schedule_text: str | None) -> str:
    days = ", ".join(settings["days"])
    lines = [
        "Build a study timetable with these constraints.",
        f"- Free-time window (study ONLY inside it): {settings['window_start']} to {settings['window_end']} (24-hour)",
        f"- Days to plan: {days}",
        f"- Preferred study session length: {settings['session_minutes']} minutes",
        f"- Break length between sessions: {settings['break_minutes']} minutes",
    ]
    if settings.get("subjects", "").strip():
        lines.append(f"- Subjects / topics to cover: {settings['subjects'].strip()}")
    if settings.get("goals", "").strip():
        lines.append(f"- Goals, exams, deadlines, preferences: {settings['goals'].strip()}")
    if settings.get("current_planner", "").strip():
        lines.append(
            "- The student's CURRENT study planner / habits (review these and suggest improvements):\n"
            + settings["current_planner"].strip()
        )
    if schedule_text:
        lines.append("\nThe student's uploaded schedule (text extracted from the file):\n" + schedule_text)
    else:
        lines.append("\nThe student's schedule is in the attached file.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Cleaning the model output
# --------------------------------------------------------------------------
def parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise PlanError("The AI did not return valid JSON.")
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise PlanError("The AI did not return valid JSON.") from exc
    if not isinstance(obj, dict):
        raise PlanError("The AI response had an unexpected format.")
    return obj


def _clean_rows(rows: Any, with_type: bool) -> tuple[list[dict], int]:
    cleaned, skipped = [], 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            skipped += 1
            continue
        day = normalize_day(row.get("day", ""))
        start, end = to_minutes(row.get("start", "")), to_minutes(row.get("end", ""))
        if day is None or start is None or end is None or end <= start:
            skipped += 1
            continue
        item = {"day": day, "start": from_minutes(start), "end": from_minutes(end)}
        if with_type:
            kind = str(row.get("activity_type", "study")).strip().lower()
            item["subject"] = str(row.get("subject", "")).strip() or "Study"
            item["activity_type"] = kind if kind in ACTIVITY_TYPES else "study"
            item["notes"] = str(row.get("notes", "")).strip()
        else:
            item["activity"] = str(row.get("activity", "")).strip() or "Busy"
        cleaned.append(item)
    cleaned.sort(key=lambda r: (DAYS.index(r["day"]), r["start"]))
    return cleaned, skipped


def normalize_plan(raw: dict) -> dict:
    timetable, skipped_t = _clean_rows(raw.get("timetable"), with_type=True)
    busy, _ = _clean_rows(raw.get("busy_blocks"), with_type=False)

    improvements = []
    for item in raw.get("improvements") if isinstance(raw.get("improvements"), list) else []:
        if isinstance(item, dict):
            title, detail = str(item.get("title", "")).strip(), str(item.get("detail", "")).strip()
            priority = str(item.get("priority", "medium")).strip().lower()
        elif isinstance(item, str):
            title, detail, priority = "Tip", item.strip(), "medium"
        else:
            continue
        if not (title or detail):
            continue
        improvements.append(
            {
                "title": title or "Tip",
                "detail": detail,
                "priority": priority if priority in PRIORITY_ORDER else "medium",
            }
        )
    improvements.sort(key=lambda i: PRIORITY_ORDER[i["priority"]])

    warnings = [str(w).strip() for w in raw.get("warnings", []) if str(w).strip()] if isinstance(
        raw.get("warnings"), list
    ) else []
    if skipped_t:
        warnings.append(f"{skipped_t} timetable row(s) from the AI were invalid and were skipped.")

    return {
        "summary": str(raw.get("summary", "")).strip(),
        "busy_blocks": busy,
        "timetable": timetable,
        "improvements": improvements,
        "warnings": warnings,
    }


def check_plan(plan: dict, window_start: str, window_end: str, days: list[str]) -> list[str]:
    """Independent sanity checks on the AI's timetable. Returns warning strings."""
    ws, we = to_minutes(window_start), to_minutes(window_end)
    issues: list[str] = []
    by_day: dict[str, list[dict]] = {}
    for row in plan["timetable"]:
        by_day.setdefault(row["day"], []).append(row)
        s, e = to_minutes(row["start"]), to_minutes(row["end"])
        if row["day"] not in days:
            issues.append(f"{row['day']} {row['start']}-{row['end']}: day was not selected.")
        if s < ws or e > we:
            issues.append(f"{row['day']} {row['start']}-{row['end']} ({row['subject']}) is outside your free-time window.")
        for busy in plan["busy_blocks"]:
            if busy["day"] == row["day"] and _overlaps(s, e, to_minutes(busy["start"]), to_minutes(busy["end"])):
                issues.append(
                    f"{row['day']} {row['start']}-{row['end']} ({row['subject']}) overlaps "
                    f"'{busy['activity']}' ({busy['start']}-{busy['end']})."
                )
    for day, rows in by_day.items():
        for prev, cur in zip(rows, rows[1:]):
            if to_minutes(cur["start"]) < to_minutes(prev["end"]):
                issues.append(f"{day}: '{prev['subject']}' and '{cur['subject']}' overlap.")
    return issues


def weekly_hours(plan: dict) -> dict[str, float]:
    """Hours per subject, computed locally from the timetable (breaks/rest excluded)."""
    totals: dict[str, float] = {}
    for row in plan["timetable"]:
        if row["activity_type"] in NON_STUDY:
            continue
        minutes = to_minutes(row["end"]) - to_minutes(row["start"])
        totals[row["subject"]] = totals.get(row["subject"], 0) + minutes / 60
    return {k: round(v, 2) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


# --------------------------------------------------------------------------
# Gemini call (with retries and a backup model)
# --------------------------------------------------------------------------
_TRANSIENT = ("503", "500", "502", "504", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED", "OVERLOADED", "HIGH DEMAND")
_QUOTA = ("429", "RESOURCE_EXHAUSTED")


def _kind(exc: Exception) -> str:
    """Classify an API error: 'transient' (retry), 'quota' (try another model) or 'fatal'."""
    text = str(exc).upper()
    if any(m in text for m in _QUOTA):
        return "quota"
    if any(m in text for m in _TRANSIENT):
        return "transient"
    return "fatal"


def call_with_fallback(
    client: Any,
    models: list[str],
    contents: list,
    config: Any,
    retries: int = 3,
    retry_delay: float = 2.0,
    sleep=time.sleep,
) -> tuple[Any, str]:
    """Call Gemini. On 503-type errors wait and retry (2s, 4s, 8s); if the model stays
    unavailable or is rate-limited, move on to the next model. Returns (response, model_used).
    Auth / bad-request / unknown-model errors on the main model are raised immediately."""
    first_error: Exception | None = None
    for index, model in enumerate(models):
        for attempt in range(retries + 1):
            try:
                return client.models.generate_content(model=model, contents=contents, config=config), model
            except Exception as exc:  # noqa: BLE001 - SDK raises several error classes
                kind = _kind(exc)
                first_error = first_error or exc
                if kind == "fatal":
                    if index == 0:
                        raise
                    break  # a bad backup model should not hide the main model's problem
                if kind == "transient" and attempt < retries:
                    sleep(retry_delay * (2**attempt))
                    continue
                break  # quota, or retries used up: try the next model
    assert first_error is not None
    raise first_error


def generate_plan(
    schedule: dict,
    settings: dict,
    api_key: str,
    model: str = DEFAULT_MODEL,
    client: Any = None,
    max_attempts: int = 2,
    fallback_model: str | None = DEFAULT_FALLBACK_MODEL,
    retries: int = 3,
    retry_delay: float = 2.0,
    sleep=time.sleep,
) -> dict:
    """Ask Gemini for a plan and return the cleaned, checked result."""
    if client is None:
        if not api_key:
            raise PlanError("No Gemini API key provided.")
        from google import genai  # imported lazily so tests can run without the SDK

        client = genai.Client(api_key=api_key)
    from google.genai import types  # noqa: E402

    prompt = build_prompt(settings, schedule["text"] if schedule["kind"] == "text" else None)
    contents: list[Any] = [prompt]
    if schedule["kind"] == "binary":
        contents.append(types.Part.from_bytes(data=schedule["data"], mime_type=schedule["mime"]))

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
    )

    models = [model]
    if fallback_model and fallback_model.strip() and fallback_model.strip() != model:
        models.append(fallback_model.strip())

    last_error: Exception | None = None
    for _ in range(max_attempts):
        response, used = call_with_fallback(client, models, contents, config, retries, retry_delay, sleep)
        try:
            plan = normalize_plan(parse_json(getattr(response, "text", "") or ""))
        except PlanError as exc:
            last_error = exc
            continue
        plan["warnings"] += check_plan(plan, settings["window_start"], settings["window_end"], settings["days"])
        plan["weekly_hours"] = weekly_hours(plan)
        plan["model_used"] = used
        return plan
    raise PlanError(f"{last_error} Please try again.")
