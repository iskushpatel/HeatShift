"""
HeatShift - a heat-aware planner for outdoor activities.

Give it a day's outdoor schedule and it tells you which activities to move,
when to move them, and why. Single file, no hardware needed.

Run locally:   streamlit run app.py
On EC2:        streamlit run app.py --server.port 8501 --server.address 0.0.0.0

Optional environment variables:
    AWS_REGION        default ap-southeast-2 (where the EC2 instance lives)
    HEATSHIFT_TABLE   default heatshift-plans (DynamoDB table, partition key plan_id)
    BEDROCK_MODEL_ID  Bedrock model / inference profile ID (can also be pasted in the sidebar)
"""
import json
import math
import os
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st

try:
    import boto3
except ImportError:  # app still works without AWS
    boto3 = None

REGION = os.getenv("AWS_REGION", "ap-southeast-2")
TABLE_NAME = os.getenv("HEATSHIFT_TABLE", "heatshift-plans")
DEFAULT_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "")

# ---------------------------------------------------------------- heat model
# Simplified thresholds on an "adjusted feels-like" temperature (deg C),
# loosely inspired by heat-index categories. Tweak freely.
LEVELS = [  # (name, upper limit, emoji, row colour)
    ("Safe", 32, "🟢", "#d8f3dc"),
    ("Caution", 38, "🟡", "#fff3bf"),
    ("Danger", 45, "🟠", "#ffd8a8"),
    ("Extreme", 999, "🔴", "#ffc9c9"),
]
EXERTION = {"Light": 0, "Moderate": 2, "Heavy": 4}      # extra body heat load, deg C
WATER_ML = {"Light": 250, "Moderate": 500, "Heavy": 750}  # baseline ml per hour


def level_of(score):
    for i, (_, limit, _, _) in enumerate(LEVELS):
        if score < limit:
            return i
    return len(LEVELS) - 1


def label(score):
    i = level_of(score)
    return f"{score:.0f}°C {LEVELS[i][2]} {LEVELS[i][0]}"


def hour_text(h):
    return f"{int(h):02d}:00"


def risk(row, intensity, boost):
    """Adjusted feels-like: forecast + simulator + effort load + UV penalty."""
    uv_penalty = 2 if row.uv >= 8 else 1 if row.uv >= 6 else 0
    return row.feels + boost + EXERTION[intensity] + uv_penalty


# ------------------------------------------------------------------- weather
@st.cache_data(ttl=1800, show_spinner="Fetching forecast...")
def get_forecast(city):
    geo = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1}, timeout=10,
    ).json()
    if not geo.get("results"):
        return None, None
    g = geo["results"][0]
    f = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": g["latitude"], "longitude": g["longitude"],
            "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature,uv_index",
            "timezone": "auto", "forecast_days": 3,
        }, timeout=10,
    ).json()["hourly"]
    df = pd.DataFrame({
        "time": pd.to_datetime(f["time"]),
        "temp": f["temperature_2m"],
        "humidity": f["relative_humidity_2m"],
        "feels": f["apparent_temperature"],
        "uv": f["uv_index"],
    })
    df["feels"] = df["feels"].fillna(df["temp"])
    df["uv"] = df["uv"].fillna(0)
    df["date"] = df["time"].dt.strftime("%Y-%m-%d")
    df["hour"] = df["time"].dt.hour
    return f"{g['name']}, {g.get('country', '')}", df


# ----------------------------------------------------------------- scheduler
def peak(grid, date, start, hours, intensity, boost):
    vals = [risk(grid[(date, h)], intensity, boost)
            for h in range(start, start + hours) if (date, h) in grid]
    return max(vals) if vals else None


def plan_activity(a, grid, boost, ok_level):
    """Return a recommendation dict for one activity row."""
    date, start = a["Day"], int(a["Start hour"])
    hours = max(1, math.ceil(float(a["Hours"])))
    inten = a["Intensity"]
    w0, w1 = int(a["Earliest"]), int(a["Latest end"])

    before = peak(grid, date, start, hours, inten, boost)
    if before is None:
        return None

    best = None  # ((peak, distance from original), start, peak)
    for s in range(w0, w1 - hours + 1):
        p = peak(grid, date, s, hours, inten, boost)
        if p is None:
            continue
        key = (round(p, 1), abs(s - start))
        if best is None or key < best[0]:
            best = (key, s, p)

    if level_of(before) <= ok_level:
        action, new_start, after = "Keep", start, before
        why = "Already within the acceptable heat range."
    elif best and best[2] < before - 0.5 and best[1] != start:
        action, new_start, after = "Move", best[1], best[2]
        why = (f"Peaks at {label(before)} at {hour_text(start)}. "
               f"Moving to {hour_text(new_start)} brings it to {label(after)}.")
    else:
        action, new_start, after = "Keep + precautions", start, before
        why = (f"Peaks at {label(before)} and no cooler slot fits the window. "
               "Shorten it, add shade breaks, or widen the window.")

    lvl = level_of(after)
    ml = min(1000, round(WATER_ML[inten] * (1 + 0.25 * lvl), -1))  # capped: more than ~1 L/hour isn't sensible advice
    hydration = f"~{ml:.0f} ml/hour" + (", shade break every 30 min" if lvl >= 2 else "")
    return {
        "name": a["Activity"], "day": date, "planned": hour_text(start),
        "action": action, "suggested": f"{hour_text(new_start)}–{hour_text(new_start + hours)}",
        "before": before, "after": after, "hydration": hydration, "why": why,
    }


# ----------------------------------------------------------------------- AWS
def bedrock_explain(model_id, rec):
    """One friendly sentence from Bedrock. Returns None on any failure."""
    cache = st.session_state.setdefault("ai_cache", {})
    facts = rec["why"] + f" Hydration advice: {rec['hydration']}."
    if (model_id, facts) in cache:
        return cache[(model_id, facts)]
    try:
        client = boto3.client("bedrock-runtime", region_name=REGION)
        prompt = (
            "You are HeatShift, a friendly heat-safety assistant. In ONE sentence "
            "(max 30 words), explain this scheduling advice to a student. Use only "
            f"these facts, do not invent numbers:\n{facts}"
        )
        r = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 120, "temperature": 0.3},
        )
        text = r["output"]["message"]["content"][0]["text"].strip()
        cache[(model_id, facts)] = text
        return text
    except Exception as e:
        st.session_state["ai_error"] = str(e)[:200]
        return None


def plans_table():
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def save_plan(city, acts_df):
    plans_table().put_item(Item={
        "plan_id": str(uuid.uuid4()),
        "created": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "activities": acts_df.to_json(orient="records"),
    })


def load_last_plan():
    items = plans_table().scan().get("Items", [])
    if not items:
        return None
    latest = max(items, key=lambda i: i["created"])
    return latest["city"], pd.DataFrame(json.loads(latest["activities"]))


# ------------------------------------------------------------------------ UI
st.set_page_config(page_title="HeatShift", page_icon="🌡️", layout="wide")
st.title("🌡️ HeatShift")
st.caption("Don't just check the temperature. Shift the plan.")

with st.sidebar:
    st.header("Settings")
    city = st.text_input("City", "Bhopal")
    boost = st.slider("Heatwave simulator (+°C)", 0, 8, 0,
                      help="Adds degrees to the forecast so you can demo a heatwave on any day.")
    ok_name = st.selectbox("Acceptable heat level", ["Safe", "Caution"], index=1)
    ok_level = [l[0] for l in LEVELS].index(ok_name)
    st.divider()
    st.subheader("AWS")
    use_ai = st.checkbox("Explain moves with Amazon Bedrock", value=False)
    model_id = st.text_input("Bedrock model ID", DEFAULT_MODEL_ID,
                             help="Copy the exact ID from the Bedrock console.")
    st.caption(f"Region: {REGION} · Table: {TABLE_NAME}")

try:
    place, forecast = get_forecast(city)
except Exception as e:
    st.error(f"Couldn't reach the forecast service: {e}")
    st.stop()
if forecast is None:
    st.error(f"Couldn't find a city called '{city}'.")
    st.stop()

dates = sorted(forecast["date"].unique())
grid = {(r.date, r.hour): r for r in forecast.itertuples()}

today = forecast[forecast["date"] == dates[0]]
c1, c2, c3 = st.columns(3)
c1.metric("Location", place)
c2.metric("Today's max temperature", f"{today['temp'].max():.0f}°C")
c3.metric("Today's max feels-like (with simulator)", f"{today['feels'].max() + boost:.0f}°C")

# ---- schedule editor
if "acts" not in st.session_state:
    st.session_state.acts = pd.DataFrame([
        ["PE class", dates[0], 11, 1, "Moderate", 6, 18],
        ["Lab setup (outdoor)", dates[0], 12, 1, "Light", 7, 18],
        ["Delivery shift", dates[0], 14, 3, "Heavy", 6, 21],
        ["Football practice", dates[0], 16, 2, "Heavy", 6, 20],
    ], columns=["Activity", "Day", "Start hour", "Hours", "Intensity", "Earliest", "Latest end"])
    st.session_state.ver = 0

st.subheader("1. Your outdoor schedule")
edited = st.data_editor(
    st.session_state.acts, num_rows="dynamic", use_container_width=True,
    key=f"editor_{st.session_state.ver}",
    column_config={
        "Day": st.column_config.SelectboxColumn(options=dates, required=True),
        "Start hour": st.column_config.NumberColumn(min_value=0, max_value=23, step=1),
        "Hours": st.column_config.NumberColumn(min_value=1, max_value=8, step=1),
        "Intensity": st.column_config.SelectboxColumn(options=list(EXERTION), required=True),
        "Earliest": st.column_config.NumberColumn("Earliest start", min_value=0, max_value=23, step=1),
        "Latest end": st.column_config.NumberColumn(min_value=1, max_value=24, step=1),
    },
)
edited = edited.rename(columns={"Earliest start": "Earliest"})

b1, b2, _ = st.columns([1, 1, 4])
if b1.button("💾 Save plan (DynamoDB)"):
    try:
        save_plan(city, edited)
        st.success("Plan saved to DynamoDB.")
    except Exception as e:
        st.error(f"Couldn't save: {str(e)[:160]}")
if b2.button("📂 Load last plan"):
    try:
        saved = load_last_plan()
        if saved is None:
            st.info("No saved plans yet.")
        else:
            _, df_saved = saved
            df_saved["Day"] = df_saved["Day"].where(df_saved["Day"].isin(dates), dates[0])
            st.session_state.acts = df_saved
            st.session_state.ver += 1
            st.rerun()
    except Exception as e:
        st.error(f"Couldn't load: {str(e)[:160]}")

# ---- recommendations
st.subheader("2. HeatShift's recommendations")
recs = []
for _, row in edited.dropna(subset=["Activity", "Day", "Start hour", "Hours",
                                    "Intensity", "Earliest", "Latest end"]).iterrows():
    rec = plan_activity(row, grid, boost, ok_level)
    if rec:
        if use_ai and model_id and boto3 and rec["action"] != "Keep":
            with st.spinner(f"Asking Bedrock about {rec['name']}..."):
                ai_text = bedrock_explain(model_id, rec)
            if ai_text:
                rec["why"] = ai_text
        recs.append(rec)

if not recs:
    st.info("Add at least one activity above.")
else:
    moved = sum(r["action"] == "Move" for r in recs)
    m1, m2, m3 = st.columns(3)
    m1.metric("Activities", len(recs))
    m2.metric("Moved to a safer slot", moved)
    m3.metric("Kept as planned", len(recs) - moved)

    out = pd.DataFrame([{
        "Activity": r["name"], "Day": r["day"], "Planned": r["planned"],
        "Action": r["action"], "Suggested": r["suggested"],
        "Peak before": label(r["before"]), "Peak after": label(r["after"]),
        "Hydration": r["hydration"], "Why": r["why"],
    } for r in recs])
    colours = [LEVELS[level_of(r["after"])][3] for r in recs]
    st.dataframe(out.style.apply(lambda row: [f"background-color: {colours[row.name]}"] * len(row), axis=1),
                 use_container_width=True, hide_index=True)

    if use_ai and st.session_state.get("ai_error"):
        st.warning("Bedrock isn't answering yet, so the built-in explanations are shown. "
                   f"Last error: {st.session_state['ai_error']}")

# ---- chart
st.subheader("3. How the day heats up")
day = st.selectbox("Day", dates)
d = forecast[forecast["date"] == day]
chart = pd.DataFrame({
    "Light effort": [risk(r, "Light", boost) for r in d.itertuples()],
    "Heavy effort": [risk(r, "Heavy", boost) for r in d.itertuples()],
    "Caution (32)": 32, "Danger (38)": 38,
}, index=d["hour"])
chart.index.name = "Hour of day"
st.line_chart(chart)

st.caption("Thresholds are a simplified, demo-grade approximation inspired by heat-index "
           "categories, not medical advice. Forecasts by Open-Meteo.")