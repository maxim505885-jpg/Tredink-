"""Read-only Telegram signal notifier for the local Tredink dashboard."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("tredink.notifier")
API_URL = os.getenv("TREDINK_API_URL", "http://freqtrade:8080").rstrip("/")
API_USER = os.getenv("TREDINK_API_USERNAME", "tredink")
API_PASSWORD = os.getenv("TREDINK_API_PASSWORD", "")
BOT_TOKEN = os.getenv("TREDINK_TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TREDINK_TELEGRAM_CHAT_ID", "")
PAIRS = [item.strip() for item in os.getenv(
    "TREDINK_PAIRS", "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT"
).split(",") if item.strip()]
EQUITY = float(os.getenv("TREDINK_EQUITY_USDT", "1000"))
RISK_FRACTION = float(os.getenv("TREDINK_RISK_PER_TRADE", "0.005"))
MAX_POSITION_FRACTION = float(os.getenv("TREDINK_MAX_POSITION_PCT", "0.10"))
MAX_POSITION_USDT = float(os.getenv("TREDINK_MAX_POSITION_USDT", "50"))
MIN_EXPECTED_MOVE_PCT = float(os.getenv("TREDINK_MIN_EXPECTED_MOVE_PCT", "0.5"))
POLL_SECONDS = max(60, int(os.getenv("TREDINK_POLL_SECONDS", "900")))
STATE_FILE = Path("/freqtrade/user_data/tredink-notifier-state.json")
VALIDATION_FILE = Path("/freqtrade/user_data/tredink-validation.json")
STRATEGY_FILE = Path("/freqtrade/user_data/strategies/TredinkForecastStrategy.py")
CONFIG_FILE = Path("/freqtrade/user_data/tredink.sample.json")
HORIZON_CANDLES = 12
TIMEFRAME_MINUTES = 15
STATUS: dict[str, object] = {
    "started": datetime.now(UTC).isoformat(),
    "last_cycle": None,
    "last_signal": None,
    "last_error": None,
    "signals_sent": 0,
}
STATUS_LOCK = threading.Lock()


def _validate_risk_settings() -> None:
    values = (EQUITY, RISK_FRACTION, MAX_POSITION_FRACTION, MAX_POSITION_USDT, MIN_EXPECTED_MOVE_PCT)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Risk settings must be finite numbers")
    if EQUITY <= 0 or MAX_POSITION_USDT <= 0:
        raise ValueError("Equity and maximum position must be greater than zero")
    if not 0 < RISK_FRACTION <= 0.05:
        raise ValueError("Risk per trade must be greater than zero and at most 5%")
    if not 0 < MAX_POSITION_FRACTION <= 1:
        raise ValueError("Maximum position percentage must be in (0, 1]")
    if MIN_EXPECTED_MOVE_PCT < 0:
        raise ValueError("Minimum expected move cannot be negative")


def _json_request(url: str, payload: dict | None = None, token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers)
    with urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def _login() -> str:
    raw = f"{API_USER}:{API_PASSWORD}".encode("utf-8")
    request = Request(
        f"{API_URL}/api/v1/token/login",
        headers={"Authorization": f"Basic {base64.b64encode(raw).decode('ascii')}"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))["access_token"]


def _send_telegram(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram token/chat ID are not configured in .env")
    payload = urlencode({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"Telegram request failed with HTTP {error.code}") from None
    except URLError:
        raise RuntimeError("Telegram network request failed") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def _load_state() -> dict[str, str]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_validation() -> dict:
    try:
        report = json.loads(VALIDATION_FILE.read_text(encoding="utf-8"))
        strategy_hash = hashlib.sha256(STRATEGY_FILE.read_bytes()).hexdigest()
        config_hash = hashlib.sha256(CONFIG_FILE.read_bytes()).hexdigest()
        if report.get("strategy_sha256") != strategy_hash or report.get("config_sha256") != config_hash:
            LOG.warning("Walk-forward report is stale for the current strategy/configuration")
            return {}
        return report
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def _format_price(value: float) -> str:
    return f"{value:,.6f}" if abs(value) < 1 else f"{value:,.2f}"


def _position_size(close: float, atr_fraction: float) -> tuple[float, float, float]:
    _validate_risk_settings()
    if not math.isfinite(close) or close <= 0:
        raise ValueError("Price must be finite and greater than zero")
    if not math.isfinite(atr_fraction) or atr_fraction < 0:
        raise ValueError("ATR must be finite and nonnegative")
    stop_fraction = min(0.03, max(0.0075, atr_fraction * 2 if atr_fraction > 0 else 0.015))
    risk_budget = EQUITY * RISK_FRACTION
    position = min(
        risk_budget / stop_fraction,
        EQUITY * MAX_POSITION_FRACTION,
        MAX_POSITION_USDT,
    )
    return position, risk_budget, stop_fraction


def _make_signal_message(pair: str, close: float, expected_pct: float, atr_pct: float) -> str:
    position, risk_budget, stop_pct = _position_size(close, atr_pct)
    stop_price = close * (1 - stop_pct)
    horizon_hours = HORIZON_CANDLES * TIMEFRAME_MINUTES / 60
    base_asset = pair.split("/")[0]
    quantity = position / close if close > 0 else 0
    return "\n".join([
        f"TREDINK · paper signal · {pair}",
        "Сторона: LONG (Spot)",
        f"Ожидаемое изменение модели за {horizon_hours:g} ч: +{expected_pct:.2f}%",
        f"Расчётный вход: около {_format_price(close)} USDT",
        f"Размер позиции (симуляция): {position:.2f} USDT ≈ {quantity:.8f} {base_asset}",
        f"Стоп для расчёта риска: {_format_price(stop_price)} USDT (−{stop_pct * 100:.2f}%)",
        f"Риск у этого размера: около {position * stop_pct:.2f} USDT (лимит {risk_budget:.2f}; капитал {EQUITY:.2f})",
        "Условия: свежая закрытая свеча, do_predict=1, порог движения пройден.",
        "Это аналитический сигнал, не команда на сделку и не гарантия результата.",
    ])


def _analyze_pair(pair: str, token: str, state: dict[str, str]) -> None:
    now = datetime.now(UTC)
    payload = {
        "pair": pair,
        "timeframe": "15m",
        "limit": 1500,
        "columns": ["date", "close", "volume", "forecast_return", "forecast_return_pct", "do_predict", "atr_pct"],
    }
    result = _json_request(f"{API_URL}/api/v1/pair_candles", payload, token)
    columns = result.get("columns", [])
    rows = result.get("data", [])
    if not rows:
        LOG.info("No candles returned for %s", pair)
        return

    latest = rows[-1]
    values = dict(zip(columns, latest))
    candle_at = datetime.fromisoformat(str(values["date"]).replace("Z", "+00:00"))
    if candle_at.tzinfo is None:
        candle_at = candle_at.replace(tzinfo=UTC)
    candle_close_at = candle_at + timedelta(minutes=TIMEFRAME_MINUTES)
    if candle_close_at > now or now - candle_close_at > timedelta(minutes=TIMEFRAME_MINUTES * 2 + 5):
        LOG.info("Skipping stale candle for %s (%s)", pair, candle_at.isoformat())
        return

    close = float(values.get("close") or 0)
    expected = float(values.get("forecast_return") or 0)
    atr_fraction = float(values.get("atr_pct") or 0)
    if (
        int(values.get("do_predict") or 0) != 1
        or not all(math.isfinite(value) for value in (close, expected, atr_fraction))
        or close <= 0
        or atr_fraction < 0
    ):
        LOG.info("Model quality gate rejected %s", pair)
        return
    if expected * 100 < MIN_EXPECTED_MOVE_PCT:
        LOG.info("Forecast below entry threshold for %s: %.3f%%", pair, expected * 100)
        return
    candle_key = candle_at.isoformat()
    if state.get(pair) == candle_key:
        LOG.info("Signal already sent for %s candle %s", pair, candle_key)
        return

    _send_telegram(_make_signal_message(pair, close, expected * 100, atr_fraction))
    state[pair] = candle_key
    _save_state(state)
    with STATUS_LOCK:
        STATUS["last_signal"] = datetime.now(UTC).isoformat()
        STATUS["signals_sent"] = int(STATUS["signals_sent"]) + 1
    LOG.info("Telegram paper signal sent for %s", pair)


def _run_cycle() -> None:
    if not API_PASSWORD:
        raise RuntimeError("TREDINK_API_PASSWORD is empty")
    if not BOT_TOKEN or not CHAT_ID:
        LOG.warning("Telegram is not configured; fill TREDINK_TELEGRAM_BOT_TOKEN and TREDINK_TELEGRAM_CHAT_ID in .env")
        return
    validation = _load_validation()
    qualified = [pair for pair in PAIRS if validation.get("pairs", {}).get(pair, {}).get("status") == "paper_eligible"]
    if not qualified:
        LOG.info("No pair passed walk-forward validation. Run scripts/tredink-backtest.ps1 before enabling signals.")
        with STATUS_LOCK:
            STATUS["last_cycle"] = datetime.now(UTC).isoformat()
        return
    token = _login()
    state = _load_state()
    for pair in qualified:
        try:
            _analyze_pair(pair, token, state)
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, RuntimeError) as error:
            LOG.exception("Analysis failed for %s", pair)
            with STATUS_LOCK:
                STATUS["last_error"] = f"{pair}: {error.__class__.__name__}"
        time.sleep(1)
    with STATUS_LOCK:
        STATUS["last_cycle"] = datetime.now(UTC).isoformat()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        with STATUS_LOCK:
            snapshot = dict(STATUS)
        validation = _load_validation()
        eligible = [pair for pair in PAIRS if validation.get("pairs", {}).get(pair, {}).get("status") == "paper_eligible"]
        snapshot.update({
            "telegram_configured": bool(BOT_TOKEN and CHAT_ID),
            "pairs": len(PAIRS),
            "validation_available": bool(validation),
            "validation_eligible_pairs": eligible,
            "poll_seconds": POLL_SECONDS,
            "equity_usdt": EQUITY,
            "risk_per_trade_pct": RISK_FRACTION * 100,
            "max_position_pct": MAX_POSITION_FRACTION * 100,
            "max_position_usdt": MAX_POSITION_USDT,
            "min_expected_move_pct": MIN_EXPECTED_MOVE_PCT,
        })
        body = json.dumps(snapshot).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/test":
            self.send_error(404)
            return
        try:
            _send_telegram("Tredink: тестовая связь работает. Сигналы приходят только в paper-режиме.")
        except (HTTPError, URLError, RuntimeError) as error:
            body = json.dumps({"ok": False, "error": "Проверьте настройки Telegram и доступ к API"}).encode("utf-8")
            self.send_response(502)
        else:
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        LOG.debug("Notifier health: " + format_string, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _validate_risk_settings()
    server = ThreadingHTTPServer(("0.0.0.0", 8600), HealthHandler)
    threading.Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    LOG.info("Notifier service is listening on internal port 8600")
    while True:
        started = time.monotonic()
        try:
            _run_cycle()
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, RuntimeError) as error:
            LOG.exception("Notifier cycle failed")
            with STATUS_LOCK:
                STATUS["last_error"] = error.__class__.__name__
        elapsed = time.monotonic() - started
        time.sleep(max(15, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
