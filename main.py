import os
import json
import logging
from datetime import datetime, timedelta

import pytz
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
FORWARD_WEBHOOK_URL = os.getenv("FORWARD_WEBHOOK_URL", "")
NY_TZ = pytz.timezone("America/New_York")
UTC_TZ = pytz.utc

FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_SECONDS = 15 * 60

_cache_events: list[tuple[str, datetime]] = []
_cache_fetched_at: datetime | None = None
_cache_status: str = "empty"


def _fetch_ff_calendar() -> tuple[list[tuple[str, datetime]], str]:
    try:
        resp = requests.get(FF_FEED_URL, timeout=10,
                            headers={"User-Agent": "MATIC-News-Gate/1.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Failed to fetch Forex Factory feed: %s", exc)
        return [], f"fetch_error: {exc}"

    events: list[tuple[str, datetime]] = []
    for item in data:
        if str(item.get("impact", "")).strip().lower() != "high":
            continue
        currency = str(item.get("country", "")).strip().upper()
        if not currency:
            continue
        date_str = item.get("date", "")
        try:
            dt_utc = datetime.fromisoformat(date_str)
            if dt_utc.tzinfo is None:
                dt_utc = UTC_TZ.localize(dt_utc)
            dt_ny = dt_utc.astimezone(NY_TZ)
        except Exception as exc:
            log.warning("Could not parse date %r: %s", date_str, exc)
            continue
        events.append((currency, dt_ny))

    log.info("Forex Factory feed loaded: %d high-impact events this week", len(events))
    return events, "ok"


def get_news_events() -> list[tuple[str, datetime]]:
    global _cache_events, _cache_fetched_at, _cache_status

    now = datetime.now(UTC_TZ)
    cache_age = (now - _cache_fetched_at).total_seconds() if _cache_fetched_at else None

    if _cache_fetched_at is None or cache_age > CACHE_TTL_SECONDS:
        events, status = _fetch_ff_calendar()
        if status == "ok":
            _cache_events = events
            _cache_fetched_at = now
            _cache_status = "live"
        else:
            if _cache_fetched_at is not None:
                age_min = int(cache_age // 60)
                _cache_status = f"stale_cache ({age_min}m old)"
                log.warning("Using stale FF cache (%dm old) — feed unreachable", age_min)
            else:
                _cache_status = "feed_unreachable"
                log.error("FF feed unreachable and no cache available — blocking all alerts")
                return []

    return _cache_events


def parse_symbol(symbol: str) -> tuple[str, str] | None:
    symbol = symbol.strip().upper()
    for suffix in (".PRO", ".FX", "=X", "_SB", "_OTC"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    if len(symbol) == 6 and symbol.isalpha():
        return symbol[:3], symbol[3:]
    return None


def calculate_window(events: list[datetime]) -> tuple[datetime, datetime] | None:
    if not events:
        return None
    earliest = min(events)
    latest = max(events)
    window_start = earliest + timedelta(minutes=5)
    window_end = latest + timedelta(hours=6)
    return window_start, window_end


def is_alert_allowed(symbol: str, now_ny: datetime) -> tuple[bool, str]:
    currencies = parse_symbol(symbol)
    if currencies is None:
        return False, f"Cannot parse symbol '{symbol}'"

    base, quote = currencies
    today_str = now_ny.strftime("%Y-%m-%d")
    all_events = get_news_events()

    relevant: list[datetime] = []
    for currency, event_dt in all_events:
        if currency in (base, quote):
            if event_dt.strftime("%Y-%m-%d") == today_str:
                relevant.append(event_dt)

    if not relevant:
        return False, (
            f"No high-impact news today ({today_str} NY) for {base} or {quote}. "
            "Alerts blocked — window has not opened."
        )

    window = calculate_window(relevant)
    if window is None:
        return False, "Could not compute trading window."

    window_start, window_end = window

    if now_ny < window_start:
        wait_secs = int((window_start - now_ny).total_seconds())
        return False, (
            f"Too early — window opens at {window_start.strftime('%H:%M')} NY "
            f"({wait_secs}s from now)."
        )

    if now_ny > window_end:
        return False, (
            f"Window closed at {window_end.strftime('%H:%M')} NY. "
            "Next window opens with tomorrow's news."
        )

    return True, (
        f"Inside window [{window_start.strftime('%H:%M')} – "
        f"{window_end.strftime('%H:%M')} NY]."
    )


def extract_symbol_from_text(body: str) -> str:
    parts = body.strip().split()
    return parts[1] if len(parts) >= 2 else ""


def forward_alert(body: str | dict, is_text: bool) -> requests.Response:
    if is_text:
        return requests.post(
            FORWARD_WEBHOOK_URL,
            data=body if isinstance(body, str) else "",
            timeout=10,
            headers={"Content-Type": "text/plain"},
        )
    return requests.post(
        FORWARD_WEBHOOK_URL,
        json=body,
        timeout=10,
        headers={"Content-Type": "application/json"},
    )


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        provided = request.args.get("secret") or request.headers.get("X-Webhook-Secret", "")
        if provided != WEBHOOK_SECRET:
            log.warning("Webhook received with invalid secret")
            return jsonify({"status": "unauthorized"}), 401

    raw_body = request.data.decode("utf-8").strip()
    content_type = request.content_type or ""
    is_text = not raw_body.startswith("{") and "json" not in content_type.lower()

    if is_text:
        symbol = extract_symbol_from_text(raw_body)
        if not symbol:
            log.warning("Could not extract symbol from plain-text body: %r", raw_body)
            return jsonify({"status": "blocked", "reason": "Could not parse symbol from message"}), 200
    else:
        try:
            payload = json.loads(raw_body)
        except Exception as exc:
            log.error("Failed to parse JSON body: %s", exc)
            return jsonify({"status": "error", "message": "Invalid JSON body"}), 400
        symbol = payload.get("symbol") or payload.get("ticker") or ""
        if not symbol:
            log.warning("JSON payload missing 'symbol' field: %s", payload)
            return jsonify({"status": "blocked", "reason": "No symbol in payload"}), 200

    now_ny = datetime.now(NY_TZ)
    allowed, reason = is_alert_allowed(symbol, now_ny)

    log.info(
        "Alert | symbol=%s | format=%s | time_ny=%s | allowed=%s | %s",
        symbol, "text" if is_text else "json",
        now_ny.strftime("%Y-%m-%d %H:%M:%S"), allowed, reason,
    )

    if not allowed:
        return jsonify({"status": "blocked", "reason": reason}), 200

    if not FORWARD_WEBHOOK_URL:
        log.error("FORWARD_WEBHOOK_URL is not set — cannot forward alert")
        return jsonify({"status": "error", "message": "FORWARD_WEBHOOK_URL not configured"}), 500

    try:
        forward_body = raw_body if is_text else payload
        resp = forward_alert(forward_body, is_text=is_text)
        log.info("Forwarded alert for %s — upstream status %s", symbol, resp.status_code)
        return jsonify({"status": "forwarded", "reason": reason, "upstream_status": resp.status_code}), 200
    except requests.RequestException as exc:
        log.error("Failed to forward alert: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 502


@app.route("/status", methods=["GET"])
def status():
    now_ny = datetime.now(NY_TZ)
    today_str = now_ny.strftime("%Y-%m-%d")
    all_events = get_news_events()

    by_currency: dict[str, list[str]] = {}
    for currency, event_dt in all_events:
        if event_dt.strftime("%Y-%m-%d") == today_str:
            by_currency.setdefault(currency, []).append(event_dt.strftime("%H:%M"))

    test_pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "GBPJPY", "EURJPY", "AUDNZD"]
    windows = {}
    for sym in test_pairs:
        allowed, reason = is_alert_allowed(sym, now_ny)
        windows[sym] = {"allowed": allowed, "reason": reason}

    cache_age_str = None
    if _cache_fetched_at:
        age_secs = int((datetime.now(UTC_TZ) - _cache_fetched_at).total_seconds())
        cache_age_str = f"{age_secs}s ago"

    return jsonify({
        "now_ny": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "data_source": {"feed": FF_FEED_URL, "status": _cache_status, "last_fetched": cache_age_str, "cache_ttl_seconds": CACHE_TTL_SECONDS},
        "news_today": by_currency,
        "windows": windows,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
