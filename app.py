"""Study Planner AI - upload your schedule, get a timetable and study tips (Streamlit + Gemini Flash)."""
import json
import os
from datetime import time

import pandas as pd
import streamlit as st

import planner

st.set_page_config(page_title="Study Planner AI", page_icon="📚", layout="wide")


# ------------------------------------------------------------------ helpers
def read_secret(name: str) -> str:
    """Read from Streamlit secrets, then environment variables. Never crashes if neither exists."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return str(value or os.getenv(name, "")).strip()


def fmt(t: time) -> str:
    return t.strftime("%H:%M")


def render_result(result: dict) -> None:
    plan = result["plan"]

    st.subheader("Your plan")
    if plan.get("model_used"):
        st.caption(f"Generated with {plan['model_used']}")
    if plan["summary"]:
        st.write(plan["summary"])
    for warning in plan["warnings"]:
        st.warning(warning)

    tab_table, tab_tips, tab_detected, tab_download = st.tabs(
        ["📅 Timetable", "💡 Improvements", "🔍 Detected schedule", "⬇️ Download"]
    )

    with tab_table:
        if not plan["timetable"]:
            st.info("No timetable was generated. Check the warnings above or try again.")
        else:
            hours = plan.get("weekly_hours", {})
            if hours:
                st.markdown("**Study hours per subject (per week)**")
                st.bar_chart(pd.Series(hours, name="Hours"))
            days_present = [d for d in planner.DAYS if any(r["day"] == d for r in plan["timetable"])]
            for tab, day in zip(st.tabs(days_present), days_present):
                with tab:
                    rows = [r for r in plan["timetable"] if r["day"] == day]
                    df = pd.DataFrame(rows).rename(
                        columns={
                            "start": "Start",
                            "end": "End",
                            "subject": "Subject",
                            "activity_type": "Type",
                            "notes": "Notes",
                        }
                    )[["Start", "End", "Subject", "Type", "Notes"]]
                    st.dataframe(df, hide_index=True)

    with tab_tips:
        if not plan["improvements"]:
            st.info("No improvement tips were returned.")
        icons = {"high": "🔴", "medium": "🟠", "low": "🟢"}
        for tip in plan["improvements"]:
            with st.container(border=True):
                st.markdown(f"{icons[tip['priority']]} **{tip['title']}** · _{tip['priority']} priority_")
                st.write(tip["detail"])

    with tab_detected:
        st.caption("Commitments the AI found in your uploaded file. If something is wrong, fix your file and run again.")
        if plan["busy_blocks"]:
            df = pd.DataFrame(plan["busy_blocks"]).rename(
                columns={"day": "Day", "start": "Start", "end": "End", "activity": "Activity"}
            )
            st.dataframe(df, hide_index=True)
        else:
            st.info("No commitments were detected in the file.")

    with tab_download:
        if plan["timetable"]:
            csv = pd.DataFrame(plan["timetable"]).to_csv(index=False)
            st.download_button("Download timetable (CSV)", csv, "study_timetable.csv", "text/csv")
        st.download_button(
            "Download full plan (JSON)",
            json.dumps(plan, indent=2, ensure_ascii=False),
            "study_plan.json",
            "application/json",
        )


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("⚙️ Settings")
    typed_key = st.text_input("Gemini API key", type="password", help="Free key: aistudio.google.com/apikey")
    api_key = typed_key.strip() or read_secret("GEMINI_API_KEY")
    model = st.text_input("Model", value=read_secret("GEMINI_MODEL") or planner.DEFAULT_MODEL)
    fallback_model = st.text_input(
        "Backup model (used if the main one is busy)",
        value=read_secret("GEMINI_FALLBACK_MODEL") or planner.DEFAULT_FALLBACK_MODEL,
    )
    if api_key:
        st.success("API key found", icon="🔑")
    else:
        st.warning("Add your Gemini API key to continue.")
    st.caption("Your uploaded file is sent to Google's Gemini API to create the plan. Don't upload anything private.")

# ------------------------------------------------------------------ main page
st.title("📚 Study Planner AI")
st.write("Upload your class / work schedule, tell me when you're free, and get a study timetable plus tips to improve your planner.")

left, right = st.columns(2)

with left:
    st.subheader("1. Your schedule")
    uploaded = st.file_uploader(
        "Upload a timetable (image, PDF, CSV, Excel or text)",
        type=planner.SUPPORTED_TYPES,
    )
    subjects = st.text_area("Subjects / topics to study", placeholder="Maths, Physics, English essay, Chemistry...")
    goals = st.text_area("Goals, exams, deadlines (optional)", placeholder="Physics exam on 20 Nov, weak in calculus, prefer mornings...")
    current_planner = st.text_area(
        "Your current study habits / planner (optional)",
        placeholder="I study 3 hours at night, mostly cram before tests...",
    )

with right:
    st.subheader("2. Your free time")
    c1, c2 = st.columns(2)
    window_start = c1.time_input("Free from", value=time(16, 0), step=900)
    window_end = c2.time_input("Free until", value=time(21, 0), step=900)
    days = st.multiselect("Days to plan", planner.DAYS, default=planner.DAYS[:5])
    session_minutes = st.slider("Study session length (minutes)", 20, 120, 50, step=5)
    break_minutes = st.slider("Break length (minutes)", 5, 30, 10, step=5)

generate = st.button("✨ Generate my timetable", type="primary")

if generate:
    st.session_state.pop("result", None)
    if not api_key:
        st.error("Please add your Gemini API key in the sidebar.")
    elif uploaded is None:
        st.error("Please upload your schedule first.")
    elif not days:
        st.error("Please select at least one day.")
    elif window_end <= window_start:
        st.error("'Free until' must be later than 'Free from'.")
    else:
        settings = {
            "window_start": fmt(window_start),
            "window_end": fmt(window_end),
            "days": [d for d in planner.DAYS if d in days],
            "session_minutes": session_minutes,
            "break_minutes": break_minutes,
            "subjects": subjects,
            "goals": goals,
            "current_planner": current_planner,
        }
        try:
            schedule = planner.load_schedule(uploaded.name, uploaded.getvalue())
            with st.spinner("Building your timetable..."):
                plan = planner.generate_plan(
                    schedule,
                    settings,
                    api_key=api_key,
                    model=model.strip() or planner.DEFAULT_MODEL,
                    fallback_model=fallback_model,
                )
            st.session_state["result"] = {"plan": plan}
        except (planner.ScheduleError, planner.PlanError) as exc:
            st.error(str(exc))
        except Exception as exc:  # network / quota / invalid key / unknown model
            message = str(exc)
            hint = ""
            if "API key" in message or "API_KEY" in message or "PERMISSION_DENIED" in message:
                hint = " Check that your API key is correct."
            elif "404" in message or "NOT_FOUND" in message:
                hint = " Check the model name in the sidebar."
            elif "503" in message or "UNAVAILABLE" in message:
                hint = " Google's servers are busy. The app already retried and tried the backup model. Wait a minute and press the button again."
            elif "429" in message or "RESOURCE_EXHAUSTED" in message:
                hint = " You hit the free-tier rate limit. Wait a minute and try again."
            st.error(f"Gemini request failed.{hint}")
            with st.expander("Technical details"):
                st.code(message)

# Results live in session_state so they survive reruns (e.g. clicking a download button).
if "result" in st.session_state:
    st.divider()
    render_result(st.session_state["result"])
