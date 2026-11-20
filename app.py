import os
import json
import time
import threading
import streamlit as st
import pandas as pd
import mplfinance as mpf
import requests
from PIL import Image
from google import genai
from google.genai import types

st.set_page_config(page_title="XAUUSD 1m AI Watcher", page_icon="📈", layout="centered")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


@st.cache_resource
def get_shared_state():
    return {
        "setups": [],
        "logs": ["🟢 1m Gold Watcher UI ready. Upload a chart to start tracking!"],
        "worker_started": False,
        "last_price": None
    }

state = get_shared_state()


def send_push_alert(title, message, priority="high", image_path=None):
    if not NTFY_TOPIC:
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    headers = {"Title": title, "Priority": priority, "Tags": "chart_with_upwards_trend,rotating_light"}
    try:
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                requests.put(url, data=f, headers={**headers, "Filename": "chart.png", "Message": message}, timeout=10)
        else:
            requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        state["logs"].insert(0, f"⚠️ Push alert error: {e}")


def parse_chart_image(img: Image.Image):
    client_ai = genai.Client(api_key=GEMINI_API_KEY)
    prompt = """
    Analyze this TradingView XAUUSD (Gold) chart screenshot.
    The trader draws a Risk-Reward position box (either grey/blue or red/green).
    - If the small Stop-Loss section is ABOVE the large Take-Profit section, direction is "SELL".
    - If the small Stop-Loss section is BELOW the large Take-Profit section, direction is "BUY".
    Read the exact Entry price (marked with a callout tag, horizontal line, or border between SL and TP boxes),
    the Stop Loss (SL) price (outer edge of the small box),
    and the Take Profit (TP) price (outer edge of the large box; estimate accurately from the Y-axis grid if not labeled).

    Return ONLY valid JSON:
    {"valid_signal": true, "direction": "BUY", "entry": 3702.24, "sl": 3699.57, "tp": 3721.01}
    """
    response = client_ai.models.generate_content(
        model="gemini-2.5-flash",
        contents=[img, prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json")
    )
    return json.loads(response.text)


def generate_setup_chart(df, setup, filename="confirm_chart.png"):
    plot_df = df.tail(45).copy()
    plot_df.columns = [c.capitalize() for c in plot_df.columns]
    hlines = dict(
        hlines=[setup["entry"], setup["sl"], setup["tp"]],
        colors=["#FFD700", "#FF4444", "#00E676"],
        linestyle=["-", "--", "--"],
        linewidths=[1.5, 1.2, 1.2]
    )
    title = f"XAUUSD 1m | {setup['direction']} CONFIRMED @ {setup['entry']} (SL: {setup['sl']} | TP: {setup['tp']})"
    mpf.plot(
        plot_df,
        type="candle",
        style="nightclouds",
        title=title,
        hlines=hlines,
        savefig=dict(fname=filename, dpi=150, bbox_inches="tight")
    )
    return filename


def check_1m_confirmation(df, setup):
    c1 = df.iloc[-2]  # Latest closed 1m candle
    c2 = df.iloc[-3]  # Previous 1m candle
    recent_high = df["high"].iloc[-6:-2].max()
    recent_low = df["low"].iloc[-6:-2].min()

    body = abs(c1["close"] - c1["open"])
    upper_wick = c1["high"] - max(c1["close"], c1["open"])
    lower_wick = min(c1["close"], c1["open"]) - c1["low"]

    if setup["direction"] == "BUY":
        choch = c1["close"] > recent_high and c1["close"] > c1["open"]
        pinbar = lower_wick >= (1.8 * max(body, 0.05)) and c1["close"] >= c1["open"]
        engulfing = (c1["close"] > c1["open"]) and (c2["close"] < c2["open"]) and (c1["close"] > c2["open"])
        return choch or pinbar or engulfing

    elif setup["direction"] == "SELL":
        choch = c1["close"] < recent_low and c1["close"] < c1["open"]
        pinbar = upper_wick >= (1.8 * max(body, 0.05)) and c1["close"] <= c1["open"]
        engulfing = (c1["close"] < c1["open"]) and (c2["close"] > c2["open"]) and (c1["close"] < c2["open"])
        return choch or pinbar or engulfing

    return False


def background_watcher():
    from tvDatafeed import TvDatafeed, Interval
    tv = None
    while True:
        if state["setups"]:
            try:
                if tv is None:
                    tv = TvDatafeed()
                df = tv.get_hist(symbol="XAUUSD", exchange="VANTAGE", interval=Interval.in_1_minute, n_bars=60)
                if df is None or df.empty:
                    df = tv.get_hist(symbol="XAUUSD", exchange="OANDA", interval=Interval.in_1_minute, n_bars=60)

                if df is not None and not df.empty:
                    current_price = float(df["close"].iloc[-1])
                    state["last_price"] = current_price
                    recent_low = float(df["low"].iloc[-3:].min())
                    recent_high = float(df["high"].iloc[-3:].max())

                    for setup in list(state["setups"]):
                        entry, sl, tp = setup["entry"], setup["sl"], setup["tp"]

                        if (setup["direction"] == "BUY" and current_price < sl) or \
                           (setup["direction"] == "SELL" and current_price > sl):
                            msg = f"Setup invalidated (SL {sl} breached before confirmation) for {setup['direction']} @ {entry}."
                            state["logs"].insert(0, f"❌ {msg}")
                            send_push_alert("XAUUSD Setup Invalidated", msg, priority="default")
                            state["setups"].remove(setup)
                            continue

                        if not setup["tapped"]:
                            if (setup["direction"] == "BUY" and recent_low <= entry + 0.80) or \
                               (setup["direction"] == "SELL" and recent_high >= entry - 0.80):
                                setup["tapped"] = True
                                msg = f"Price tapped {entry} ({setup['direction']}). Watching 1m candle close for CHoCH / rejection..."
                                state["logs"].insert(0, f"👀 {msg}")
                                send_push_alert("👀 Gold Tapped Zone!", msg, priority="high")

                        if setup["tapped"] and check_1m_confirmation(df, setup):
                            chart_file = generate_setup_chart(df, setup)
                            msg = (
                                f"{setup['direction']} GOLD CONFIRMED!\n"
                                f"Live Price: {current_price:.2f} | Entry: {entry} | SL: {sl} | TP: {tp}"
                            )
                            state["logs"].insert(0, f"🚨 {msg}")
                            send_push_alert(f"🚨 {setup['direction']} XAUUSD NOW!", msg, priority="urgent", image_path=chart_file)
                            state["setups"].remove(setup)
            except Exception as e:
                tv = None
                state["logs"].insert(0, f"⚠️ Feed reconnecting: {e}")
        time.sleep(15)


if not state["worker_started"]:
    t = threading.Thread(target=background_watcher, daemon=True)
    t.start()
    state["worker_started"] = True

# ================= MOBILE UI =================
st.title("📈 XAUUSD 1m AI Watcher")
st.caption("Upload signal screenshot from Telegram • Auto-maps on 1m Gold • Alerts on CHoCH / Candle Confirmation")

if state["last_price"]:
    st.metric("Live XAUUSD (1m)", f"${state['last_price']:.2f}")

uploaded_file = st.file_uploader("Upload Signal Chart Screenshot", type=["png", "jpg", "jpeg"])

if uploaded_file is not None:
    img = Image.open(uploaded_file)
    st.image(img, caption="Uploaded Signal", use_container_width=True)

    if st.button("🔍 Scan & Activate 1m Watcher", type="primary", use_container_width=True):
        with st.spinner("Reading Entry, SL & TP from chart..."):
            try:
                data = parse_chart_image(img)
                if data.get("valid_signal"):
                    st.session_state["parsed"] = data
                else:
                    st.error("Could not detect a valid Risk-Reward box in this image.")
            except Exception as e:
                st.error(f"AI Scan Error: {e}")

if "parsed" in st.session_state:
    d = st.session_state["parsed"]
    st.subheader("Verify Extracted Levels")
    col1, col2 = st.columns(2)
    direction = col1.selectbox("Direction", ["BUY", "SELL"], index=0 if d["direction"].upper() == "BUY" else 1)
    entry = col2.number_input("Entry Level", value=float(d["entry"]), format="%.2f")
    sl = col1.number_input("Stop Loss (SL)", value=float(d["sl"]), format="%.2f")
    tp = col2.number_input("Take Profit (TP)", value=float(d["tp"]), format="%.2f")

    if st.button("✅ Start Tracking on 1m Chart", use_container_width=True):
        new_setup = {"direction": direction, "entry": entry, "sl": sl, "tp": tp, "tapped": False}
        state["setups"].append(new_setup)
        state["logs"].insert(0, f"📥 Tracking {direction} @ {entry} (SL: {sl} | TP: {tp})")
        send_push_alert("📥 Signal Active", f"Watching 1m XAUUSD for {direction} @ {entry} (SL: {sl}, TP: {tp})", priority="default")
        del st.session_state["parsed"]
        st.success("Setup added! You'll get a push alert on your phone when 1m confirmation triggers.")

st.divider()
st.subheader(f"Active 1m Watchlist ({len(state['setups'])})")
for i, s in enumerate(state["setups"]):
    status = "👀 Zone Tapped (Waiting 1m Close)" if s["tapped"] else "⏳ Waiting for Price to Reach Zone"
    st.info(f"**{s['direction']} XAUUSD** | Entry: `{s['entry']}` | SL: `{s['sl']}` | TP: `{s['tp']}`\n\nStatus: {status}")
    if st.button(f"Remove #{i+1}", key=f"del_{i}"):
        state["setups"].pop(i)
        st.rerun()

st.subheader("Live Activity Log")
for log in state["logs"][:10]:
    st.text(log)
