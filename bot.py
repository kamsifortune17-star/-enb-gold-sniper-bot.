import os
import json
import requests
from pathlib import Path

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://api.twelvedata.com"
STATE_FILE = Path("state.json")

# For this bot, one XAUUSD pip is defined as $0.10.
PIP_SIZE = 0.10
MIN_SCORE = 80


def get_candles(interval, outputsize=100):
    response = requests.get(
        f"{BASE_URL}/time_series",
        params={
            "symbol": "XAU/USD",
            "interval": interval,
            "outputsize": outputsize,
            "apikey": API_KEY,
            "format": "JSON",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if "values" not in data:
        raise RuntimeError(
            f"Market data unavailable: {data.get('message', data)}"
        )

    candles = [
        {
            "time": c["datetime"],
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
        }
        for c in reversed(data["values"])
    ]

    if len(candles) < 25:
        raise RuntimeError(f"Insufficient {interval} candles")

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
    # Exclude the newest candle because it may still be forming.
    closed = candles[:-1]
    highs, lows = swing_points(closed)

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


def find_order_block(candles, side):
    # Search for the latest opposing candle before confirmation.
    history = candles[-9:-2]

    for candle in reversed(history):
        if side == "BUY" and candle["close"] < candle["open"]:
            return {
                "low": candle["low"],
                "high": candle["open"],
                "time": candle["time"],
            }

        if side == "SELL" and candle["close"] > candle["open"]:
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
    STATE_FILE.write_text(json.dumps(state, indent=2))


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
        raise RuntimeError("Telegram rejected the message")


def format_signal(side, entry, sl, tp1, tp2, score):
    if side == "BUY":
        sl_pips = round((entry - sl) / PIP_SIZE)
        tp1_pips = round((tp1 - entry) / PIP_SIZE)
        tp2_pips = round((tp2 - entry) / PIP_SIZE)
        emoji = "🟢"
        structure = "Bullish"
        sweep = "previous low"
        engulf = "Bullish"
        block = "Bullish"
    else:
        sl_pips = round((sl - entry) / PIP_SIZE)
        tp1_pips = round((entry - tp1) / PIP_SIZE)
        tp2_pips = round((entry - tp2) / PIP_SIZE)
        emoji = "🔴"
        structure = "Bearish"
        sweep = "previous high"
        engulf = "Bearish"
        block = "Bearish"

    return (
        f"🎯 ENB GOLD SNIPER — M5\n\n"
        f"Status: SIGNAL ONLY — execute manually\n"
        f"Symbol: XAUUSD\n"
        f"Side: {side} {emoji}\n\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {sl:.2f} "
        f"({'-' if side == 'BUY' else '+'}{sl_pips} pips)\n"
        f"TP1: {tp1:.2f} "
        f"({'+' if side == 'BUY' else '-'}{tp1_pips} pips)\n"
        f"TP2: {tp2:.2f} "
        f"({'+' if side == 'BUY' else '-'}{tp2_pips} pips)\n\n"
        f"Setup Score: {score}/100\n\n"
        f"Confirmation:\n"
        f"✅ Liquidity Sweep — {sweep}\n"
        f"✅ {engulf} Engulfing Bar\n"
        f"✅ {block} Order Block\n"
        f"✅ M15 {structure} Structure\n"
        f"✅ M1 Entry Refinement\n\n"
        f"Risk-to-Reward: 1:2 (TP1) | 1:4 (TP2)"
    )


def main():
    m15 = get_candles("15min")
    m5 = get_candles("5min")
    m1 = get_candles("1min")

    # Exclude the newest potentially incomplete candle.
    signal = m5[-2]
    previous = m5[-3]
    prior = m5[-12:-2]
    m1_signal = m1[-2]

    if len(prior) < 8:
        print("Insufficient history for liquidity analysis.")
        return

    trend = market_structure(m15)
    side = engulfing(signal, previous)

    if side is None:
        print("No engulfing confirmation.")
        return

    # The sweep must break a previous liquidity level
    # and close back inside the previous range.
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
        print("No confirmed liquidity sweep.")
        return

    if trend != ("BULLISH" if side == "BUY" else "BEARISH"):
        print("M15 structure does not confirm the direction.")
        return

    ob = find_order_block(m5[:-1], side)

    if ob is None:
        print("No suitable opposing candle for an order block.")
        return

    # Require the last completed M1 candle to touch the zone.
    touched = (
        m1_signal["low"] <= ob["high"]
        and m1_signal["high"] >= ob["low"]
    )

    if not touched:
        print("Waiting for price to reach the order block.")
        return

    entry = round((ob["low"] + ob["high"]) / 2, 2)

    # Use the more conservative invalidation level.
    if side == "BUY":
        sl = round(min(signal["low"], ob["low"]) - PIP_SIZE, 2)
        risk = entry - sl
        if risk <= 0:
            print("Invalid BUY risk calculation.")
            return
        tp1 = round(entry + 2 * risk, 2)
        tp2 = round(entry + 4 * risk, 2)

    else:
        sl = round(max(signal["high"], ob["high"]) + PIP_SIZE, 2)
        risk = sl - entry
        if risk <= 0:
            print("Invalid SELL risk calculation.")
            return
        tp1 = round(entry - 2 * risk, 2)
        tp2 = round(entry - 4 * risk, 2)

    # Score reflects the quality of the defined conditions.
    body = abs(signal["close"] - signal["open"])
    candle_range = signal["high"] - signal["low"]

    score = 80
    if candle_range > 0 and body / candle_range >= 0.65:
        score += 5
    if ob["high"] > ob["low"]:
        score += 5
    if abs(entry - (ob["low"] + ob["high"]) / 2) < 0.01:
        score += 5
    if trend in ("BULLISH", "BEARISH"):
        score += 5

    score = min(score, 100)

    if score < MIN_SCORE:
        print(f"Setup score {score}/100 is below threshold.")
        return

    # Unique key prevents repeat alerts for the same M5 setup.
    key = f"{signal['time']}:{side}"
    state = load_state()

    if state.get("last_signal") == key:
        print("Duplicate signal prevented.")
        return

    message = format_signal(
        side, entry, sl, tp1, tp2, score
    )

    send_telegram(message)

    state["last_signal"] = key
    save_state(state)

    print(f"{side} signal sent. Score: {score}/100")


if __name__ == "__main__":
    main()
