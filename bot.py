import os
import json
from pathlib import Path

import pandas as pd
import yfinance as yf


BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "GC=F"
STATE_FILE = Path("state.json")

# One pip is defined here as $0.10.
PIP_SIZE = 0.10

INTERVALS = {
    "1min": ("1m", 1),
    "5min": ("5m", 5),
    "15min": ("15m", 15),
}


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

    # Exclude candles that have not finished forming.
    now = (
        pd.Timestamp.now(tz=df.index.tz)
        if df.index.tz is not None
        else pd.Timestamp.now()
    )

    candle_end = df.index + pd.Timedelta(minutes=minutes)
    df = df.loc[candle_end <= now]

    if len(df) < 25:
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
        f"{interval}: {len(candles)} completed candles. "
        f"Latest: {latest['time']} | "
        f"O={latest['open']:.2f} "
        f"H={latest['high']:.2f} "
        f"L={latest['low']:.2f} "
        f"C={latest['close']:.2f}"
    )

    return candles


def swing_points(candles, radius=2):
    highs, lows = [], []

    for i in range(radius, len(candles) - radius):
        candle = candles[i]

        if all(
            candle["high"] > candles[j]["high"]
            for j in range(i - radius, i + radius + 1)
            if j != i
        ):
            highs.append((i, candle["high"]))

        if all(
            candle["low"] < candles[j]["low"]
            for j in range(i - radius, i + radius + 1)
            if j != i
        ):
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


def find_order_block(candles_before_signal, side):
    # Search the candles before the engulfing candle.
    history = candles_before_signal[-8:]

    for candle in reversed(history):
        if (
            side == "BUY"
            and candle["close"] < candle["open"]
        ):
            return {
                "low": candle["low"],
                "high": candle["open"],
                "time": candle["time"],
            }

        if (
            side == "SELL"
            and candle["close"] > candle["open"]
        ):
            return {
                "low": candle["open"],
                "high": candle["high"],
                "time": candle["time"],
            }

    return None


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2)
    )


def send_telegram(message):
    import requests

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


def format_signal(side, entry, sl, tp1, tp2):
    if side == "BUY":
        sl_pips = round((entry - sl) / PIP_SIZE)
        tp1_pips = round((tp1 - entry) / PIP_SIZE)
        tp2_pips = round((tp2 - entry) / PIP_SIZE)

        emoji = "🟢"
        structure = "Bullish"
        sweep = "Previous low"
        engulf = "Bullish"
        block = "Bullish"

    else:
        sl_pips = round((sl - entry) / PIP_SIZE)
        tp1_pips = round((entry - tp1) / PIP_SIZE)
        tp2_pips = round((entry - tp2) / PIP_SIZE)

        emoji = "🔴"
        structure = "Bearish"
        sweep = "Previous high"
        engulf = "Bearish"
        block = "Bearish"

    return (
        f"🎯 ENB GOLD SNIPER — M5\n\n"
        f"Status: SIGNAL ONLY — execute manually\n"
        f"Data Source: Yahoo Finance Gold Futures (GC=F)\n"
        f"Symbol: XAUUSD — verify against your broker\n"
        f"Side: {side} {emoji}\n\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {sl:.2f} "
        f"({'-' if side == 'BUY' else '+'}{sl_pips} pips)\n"
        f"TP1: {tp1:.2f} "
        f"({'+' if side == 'BUY' else '-'}{tp1_pips} pips)\n"
        f"TP2: {tp2:.2f} "
        f"({'+' if side == 'BUY' else '-'}{tp2_pips} pips)\n\n"
        f"Confirmation:\n"
        f"✅ Liquidity Sweep — {sweep}\n"
        f"✅ {engulf} Engulfing Bar\n"
        f"✅ {block} Order Block\n"
        f"✅ M15 {structure} Structure\n"
        f"✅ M1 Entry Refinement\n\n"
        f"Risk-to-Reward: 1:2 (TP1) | 1:4 (TP2)\n"
        f"Note: Confirm all prices on your broker's chart."
    )


def main():
    print("ENB Gold Sniper started.")
    print(f"Market data source: Yahoo Finance ({SYMBOL})")

    m15 = get_candles("15min")
    m5 = get_candles("5min")
    m1 = get_candles("1min")

    # Use the latest completed M5 candle.
    signal = m5[-1]
    previous = m5[-2]
    prior = m5[-12:-2]

    # Use the latest completed M1 candle for entry refinement.
    m1_signal = m1[-1]

    print(f"Checking M5 candle: {signal['time']}")

    if len(prior) < 8:
        print("Rejected: insufficient history for liquidity analysis.")
        return

    trend = market_structure(m15)

    print(f"M15 market structure: {trend}")

    side = engulfing(signal, previous)

    if side is None:
        print(
            "Rejected: the latest completed M5 candle is not "
            "a confirmed bullish or bearish engulfing candle."
        )
        print(
            f"Current M5: O={signal['open']:.2f}, "
            f"C={signal['close']:.2f}; "
            f"Previous M5: O={previous['open']:.2f}, "
            f"C={previous['close']:.2f}"
        )
        return

    print(f"Engulfing direction: {side}")

    if side == "BUY":
        previous_low = min(c["low"] for c in prior)

        swept = (
            signal["low"] < previous_low
            and signal["close"] > previous_low
        )

    else:
        previous_high = max(c["high"] for c in prior)

        swept = (
            signal["high"] > previous_high
            and signal["close"] < previous_high
        )

    if not swept:
        print("Rejected: no confirmed liquidity sweep.")
        return

    print("Liquidity sweep confirmed.")

    expected_trend = (
        "BULLISH" if side == "BUY" else "BEARISH"
    )

    if trend != expected_trend:
        print(
            f"Rejected: signal is {side}, but M15 structure "
            f"is {trend}, not {expected_trend}."
        )
        return

    ob = find_order_block(m5[:-1], side)

    if ob is None:
        print("Rejected: no suitable opposing candle for an order block.")
        return

    print(
        f"Order block found: {ob['low']:.2f}–{ob['high']:.2f}"
    )

    touched = (
        m1_signal["low"] <= ob["high"]
        and m1_signal["high"] >= ob["low"]
    )

    if not touched:
        print(
            "Rejected for now: the latest completed M1 candle "
            "has not touched the order-block zone."
        )
        print(
            f"Latest M1 range: {m1_signal['low']:.2f}–"
            f"{m1_signal['high']:.2f}"
        )
        return

    print("M1 entry refinement confirmed.")

    entry = round((ob["low"] + ob["high"]) / 2, 2)

    if side == "BUY":
        sl = round(
            min(signal["low"], ob["low"]) - PIP_SIZE,
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
            max(signal["high"], ob["high"]) + PIP_SIZE,
            2,
        )

        risk = sl - entry

        if risk <= 0:
            print("Rejected: invalid SELL risk calculation.")
            return

        tp1 = round(entry - 2 * risk, 2)
        tp2 = round(entry - 4 * risk, 2)

    # No setup-score calculation or score threshold.
    key = f"{signal['time']}:{side}"
    state = load_state()

    if state.get("last_signal") == key:
        print("Duplicate signal prevented.")
        return

    message = format_signal(
        side,
        entry,
        sl,
        tp1,
        tp2,
    )

    send_telegram(message)

    state["last_signal"] = key
    save_state(state)

    print(f"{side} signal sent successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"BOT ERROR: {type(exc).__name__}: {exc}")
        raise
