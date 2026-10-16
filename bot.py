import os
import json
from pathlib import Path
import pandas as pd
import yfinance as yf
import requests
# ============================================================
# ENB GOLD SNIPER
# M5 Liquidity Sweep + Engulfing
# M15 Market Structure + Order Block + M1 Rejection
#
# Data source: Yahoo Finance GC=F gold futures.
# GC=F is NOT your broker's exact XAUUSD feed.
# Signals require manual verification.
# ============================================================
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SYMBOL = "GC=F"
STATE_FILE = Path("state.json")
# Price increment used for protective SL buffers.
# Verify the increment against your broker before trading.
PRICE_BUFFER = 0.10
# Strategy settings.
SWEEP_LOOKBACK = 10
ENGULF_SEARCH_BARS = 6
MAX_SETUP_AGE_MINUTES = 20
MAX_ENTRY_DISTANCE = 1.00
INTERVALS = {
    "1min": ("1m", 1),
    "5min": ("5m", 5),
    "15min": ("15m", 15),
}
# ============================================================
# DATA
# ============================================================
def get_candles(interval):
    yahoo_interval, minutes = INTERVALS[interval]
    ticker = yf.Ticker(SYMBOL)
    df = ticker.history(
        period="5d",
        interval=yahoo_interval,
        auto_adjust=False,
        actions=False,
    )
    if df is None or df.empty:
        raise RuntimeError(
            f"Yahoo Finance returned no data for {interval}."
        )
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    now = (
        pd.Timestamp.now(tz=df.index.tz)
        if df.index.tz is not None
        else pd.Timestamp.now()
    )
    # Exclude candles that have not finished forming.
    candle_end = df.index + pd.Timedelta(minutes=minutes)
    df = df.loc[candle_end <= now]
    if len(df) < 30:
        raise RuntimeError(
            f"Insufficient completed {interval} candles: {len(df)}."
        )
    candles = []
    for timestamp, row in df.iterrows():
        candles.append({
            "time": timestamp.isoformat(),
            "open": float(row["Open"]),
            "high": float(row["High"]),
            "low": float(row["Low"]),
            "close": float(row["Close"]),
        })
    latest = candles[-1]
    print(
        f"{interval}: {len(candles)} completed candles | "
        f"{latest['time']} | "
        f"O={latest['open']:.2f} "
        f"H={latest['high']:.2f} "
        f"L={latest['low']:.2f} "
        f"C={latest['close']:.2f}"
    )
    return candles
# ============================================================
# STATE / DUPLICATE PROTECTION
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        state = json.loads(STATE_FILE.read_text())
        if not isinstance(state, dict):
            return {}
        return state
    except (json.JSONDecodeError, OSError):
        return {}
def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )
def already_sent(state, signal_key):
    sent = state.get("sent_signals", [])
    if not isinstance(sent, list):
        sent = []
    return signal_key in sent
def remember_signal(state, signal_key):
    sent = state.get("sent_signals", [])
    if not isinstance(sent, list):
        sent = []
    sent.append(signal_key)
    # Keep the most recent 100 signal identifiers.
    state["sent_signals"] = sent[-100:]
    save_state(state)
# ============================================================
# CANDLE PATTERNS
# ============================================================
def engulfing(candle, previous):
    bullish = (
        candle["close"] > candle["open"]
        and previous["close"] < previous["open"]
        and candle["open"] <= previous["close"]
        and candle["close"] >= previous["open"]
    )
    bearish = (
        candle["close"] < candle["open"]
        and previous["close"] > previous["open"]
        and candle["open"] >= previous["close"]
        and candle["close"] <= previous["open"]
    )
    if bullish:
        return "BUY"
    if bearish:
        return "SELL"
    return None
def rejection_confirmation(candle, side):
    """
    Require a directional candle with a rejection wick.
    This is a simple rule-based confirmation, not a guarantee.
    """
    candle_range = candle["high"] - candle["low"]
    if candle_range <= 0:
        return False
    body = abs(candle["close"] - candle["open"])
    upper_wick = (
        candle["high"] - max(candle["open"], candle["close"])
    )
    lower_wick = (
        min(candle["open"], candle["close"]) - candle["low"]
    )
    if side == "BUY":
        return (
            candle["close"] > candle["open"]
            and lower_wick >= body * 0.5
            and candle["close"] >= (
                candle["low"] + candle_range * 0.60
            )
        )
    return (
        candle["close"] < candle["open"]
        and upper_wick >= body * 0.5
        and candle["close"] <= (
            candle["high"] - candle_range * 0.60
        )
    )
# ============================================================
# MARKET STRUCTURE
# ============================================================
def swing_points(candles, radius=2):
    highs = []
    lows = []
    for i in range(radius, len(candles) - radius):
        candle = candles[i]
        high_values = [
            candles[j]["high"]
            for j in range(i - radius, i + radius + 1)
            if j != i
        ]
        low_values = [
            candles[j]["low"]
            for j in range(i - radius, i + radius + 1)
            if j != i
        ]
        if all(candle["high"] > h for h in high_values):
            highs.append((i, candle["high"]))
        if all(candle["low"] < low for low in low_values):
            lows.append((i, candle["low"]))
    return highs, lows
def market_structure(candles):
    highs, lows = swing_points(candles)
    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"
    higher_highs = highs[-1][1] > highs[-2][1]
    higher_lows = lows[-1][1] > lows[-2][1]
    lower_highs = highs[-1][1] < highs[-2][1]
    lower_lows = lows[-1][1] < lows[-2][1]
    if higher_highs and higher_lows:
        return "BULLISH"
    if lower_highs and lower_lows:
        return "BEARISH"
    return "RANGING"
# ============================================================
# LIQUIDITY SWEEP
# ============================================================
def find_sweep(candles, side, before_index):
    """
    Find a sweep on a completed candle BEFORE the engulfing
    candle. The swept level is calculated from earlier candles.
    """
    earliest = max(1, before_index - SWEEP_LOOKBACK)
    for i in range(before_index - 1, earliest - 1, -1):
        start = max(0, i - SWEEP_LOOKBACK)
        history = candles[start:i]
        if len(history) < 5:
            continue
        candle = candles[i]
        if side == "BUY":
            previous_low = min(
                c["low"] for c in history
            )
            if (
                candle["low"] < previous_low
                and candle["close"] > previous_low
            ):
                return {
                    "index": i,
                    "level": previous_low,
                    "time": candle["time"],
                }
        else:
            previous_high = max(
                c["high"] for c in history
            )
            if (
                candle["high"] > previous_high
                and candle["close"] < previous_high
            ):
                return {
                    "index": i,
                    "level": previous_high,
                    "time": candle["time"],
                }
    return None
# ============================================================
# ORDER BLOCK
# ============================================================
def find_order_block(candles, sweep_index, signal_index, side):
    """
    Find the nearest opposing candle between the sweep and
    the engulfing confirmation.
    This is a practical candle-zone definition. It does not
    establish that the zone is institutionally validated.
    """
    # Prefer candles after the sweep and before confirmation.
    start = max(0, sweep_index)
    end = signal_index
    for i in range(end - 1, start - 1, -1):
        candle = candles[i]
        if side == "BUY" and candle["close"] < candle["open"]:
            return {
                "low": candle["low"],
                "high": candle["open"],
                "time": candle["time"],
                "index": i,
            }
        if side == "SELL" and candle["close"] > candle["open"]:
            return {
                "low": candle["open"],
                "high": candle["high"],
                "time": candle["time"],
                "index": i,
            }
    # If no opposing candle exists in that interval, reject.
    return None
# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
        },
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(
            "Telegram rejected the message."
        )
def format_signal(side, entry, sl, tp1, tp2, structure, sweep, ob):
    if side == "BUY":
        emoji = "🟢"
        sl_distance = entry - sl
        tp1_distance = tp1 - entry
        tp2_distance = tp2 - entry
    else:
        emoji = "🔴"
        sl_distance = sl - entry
        tp1_distance = entry - tp1
        tp2_distance = entry - tp2
    return (
        f"🎯 ENB GOLD SNIPER — M5\n\n"
        f"Status: SIGNAL ONLY — EXECUTE MANUALLY\n"
        f"Data: Yahoo Finance Gold Futures (GC=F)\n"
        f"Symbol reference: XAUUSD — verify on your broker\n\n"
        f"Direction: {side} {emoji}\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {sl:.2f} | Distance: {sl_distance:.2f}\n"
        f"TP1: {tp1:.2f} | Distance: {tp1_distance:.2f}\n"
        f"TP2: {tp2:.2f} | Distance: {tp2_distance:.2f}\n\n"
        f"CONFIRMATIONS\n"
        f"✅ Liquidity sweep: {sweep['time']}\n"
        f"✅ {side} engulfing candle\n"
        f"✅ M15 structure: {structure}\n"
        f"✅ Order block: {ob['low']:.2f}–{ob['high']:.2f}\n"
        f"✅ M1 rejection confirmation\n\n"
        f"Risk-to-reward: 1:2 (TP1) | 1:4 (TP2)\n"
        f"Important: GC=F prices may differ from broker XAUUSD.\n"
        f"Verify entry, SL and targets on your broker chart."
    )
# ============================================================
# SETUP SEARCH
# ============================================================
def find_latest_setup(m5, m15, m1):
    trend = market_structure(m15)
    print(f"M15 market structure: {trend}")
    if trend not in ("BULLISH", "BEARISH"):
        print("Rejected: M15 structure is not directional.")
        return None
    expected_side = "BUY" if trend == "BULLISH" else "SELL"
    latest_m5_index = len(m5) - 1
    # Search recent completed M5 candles, newest first.
    first_index = max(
        1,
        latest_m5_index - ENGULF_SEARCH_BARS + 1,
    )
    for signal_index in range(
        latest_m5_index,
        first_index - 1,
        -1,
    ):
        signal = m5[signal_index]
        previous = m5[signal_index - 1]
        side = engulfing(signal, previous)
        if side is None:
            continue
        if side != expected_side:
            print(
                f"Rejected {signal['time']}: engulfing direction "
                f"{side} conflicts with M15 structure {trend}."
            )
            continue
        print(
            f"Candidate {side} engulfing at {signal['time']}."
        )
        sweep = find_sweep(
            m5,
            side,
            signal_index,
        )
        if sweep is None:
            print(
                "Rejected: no preceding liquidity sweep found "
                "within the configured lookback."
            )
            continue
        signal_time = pd.Timestamp(signal["time"])
        sweep_time = pd.Timestamp(sweep["time"])
        if sweep_time >= signal_time:
            print("Rejected: sweep did not precede confirmation.")
            continue
        age_minutes = (
            pd.Timestamp.now(tz=signal_time.tz)
            - signal_time
        ).total_seconds() / 60
        if age_minutes < 0 or age_minutes > MAX_SETUP_AGE_MINUTES:
            print(
                f"Rejected: setup is stale ({age_minutes:.1f} minutes)."
            )
            continue
        ob = find_order_block(
            m5,
            sweep["index"],
            signal_index,
            side,
        )
        if ob is None:
            print("Rejected: no suitable order-block candle found.")
            continue
        # Require a completed M1 rejection candle AFTER the M5
        # confirmation, and require price to interact with the zone.
        confirmation = None
        for candle in reversed(m1):
            candle_time = pd.Timestamp(candle["time"])
            if candle_time <= signal_time:
                break
            touched = (
                candle["low"] <= ob["high"]
                and candle["high"] >= ob["low"]
            )
            if touched and rejection_confirmation(candle, side):
                confirmation = candle
                break
        if confirmation is None:
            print(
                "Rejected for now: no post-setup M1 rejection "
                "confirmation inside the order-block zone."
            )
            continue
        return {
            "side": side,
            "signal": signal,
            "sweep": sweep,
            "ob": ob,
            "confirmation": confirmation,
            "trend": trend,
        }
    print("No fresh, fully confirmed sniper setup.")
    return None
# ============================================================
# MAIN
# ============================================================
def main():
    print("ENB Gold Sniper started.")
    print(f"Market data: Yahoo Finance ({SYMBOL})")
    print("Mode: SNIPER — M1 confirmation required.")
    m15 = get_candles("15min")
    m5 = get_candles("5min")
    m1 = get_candles("1min")
    setup = find_latest_setup(m5, m15, m1)
    if setup is None:
        return
    side = setup["side"]
    signal = setup["signal"]
    ob = setup["ob"]
    sweep = setup["sweep"]
    # The midpoint of the zone is only a proposed entry.
    entry = round((ob["low"] + ob["high"]) / 2, 2)
    # Use the latest available completed M1 close as a proxy
    # for current price. It is not a live broker quote.
    reference_price = m1[-1]["close"]
    distance = abs(reference_price - entry)
    if distance > MAX_ENTRY_DISTANCE:
        print(
            f"Rejected: entry is too far from reference price. "
            f"Entry={entry:.2f}, reference={reference_price:.2f}, "
            f"distance={distance:.2f}."
        )
        return
    if side == "BUY":
        sl = round(
            min(signal["low"], ob["low"]) - PRICE_BUFFER,
            2,
        )
        risk = entry - sl
        if risk <= 0:
            print("Rejected: invalid BUY risk calculation.")
            return
        tp1 = round(entry + 2 * risk, 2)
        tp2 = round(entry + 4 * risk, 2)
    else:
        sl = round(
            max(signal["high"], ob["high"]) + PRICE_BUFFER,
            2,
        )
        risk = sl - entry
        if risk <= 0:
            print("Rejected: invalid SELL risk calculation.")
            return
        tp1 = round(entry - 2 * risk, 2)
        tp2 = round(entry - 4 * risk, 2)
    # Identify the setup by its confirmation candle and direction.
    key = (
        f"{side}:"
        f"{signal['time']}:"
        f"{setup['confirmation']['time']}"
    )
    state = load_state()
    if already_sent(state, key):
        print("Duplicate signal prevented.")
        return
    message = format_signal(
        side=side,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        structure=setup["trend"],
        sweep=sweep,
        ob=ob,
    )
    # Send first. Persist the key only after Telegram confirms.
    send_telegram(message)
    remember_signal(state, key)
    print(f"{side} sniper signal sent successfully.")
if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BOT ERROR: {type(exc).__name__}: {exc}")
        raise
