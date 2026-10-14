"""Local web server and server-side Alpha Vantage FX proxy."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
IS_RENDER = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_HOSTNAME"))
HOST = "0.0.0.0" if IS_RENDER else "127.0.0.1"
PORT = int(os.environ.get("PORT", "8000"))
API_URL = "https://www.alphavantage.co/query"
ALLOWED_PAIRS = {
    ("EUR", "USD"),
    ("GBP", "USD"),
    ("USD", "JPY"),
    ("USD", "CHF"),
    ("AUD", "USD"),
    ("USD", "CAD"),
}
TIMEFRAMES = {"1H", "4H", "1D", "1W"}
STATIC_FILES = {"index.html", "styles.css", "app.js", "README.md"}
CACHE: dict[tuple[str, str, str], tuple[float, dict[str, object]]] = {}
UPSTREAM_CALLS: list[float] = []
UPSTREAM_CALLS_LOCK = threading.Lock()
MAX_UPSTREAM_CALLS_PER_DAY = 20
UPSTREAM_CALL_WINDOW_SECONDS = 24 * 60 * 60


class APIError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def load_local_environment() -> None:
    """Load ALPHAVANTAGE_API_KEY from an optional, git-ignored .env file."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip("\"'")
        if name == "ALPHAVANTAGE_API_KEY" and value and not os.environ.get(name, "").strip():
            os.environ[name] = value


def parse_time_series(payload: dict[str, object], timeframe: str) -> tuple[list[dict[str, object]], str]:
    series_names = {
        "1H": "Time Series FX (60min)",
        "4H": "Time Series FX (60min)",
        "1D": "Time Series FX (Daily)",
        "1W": "Time Series FX (Weekly)",
    }
    series_name = series_names[timeframe]
    raw_series = payload.get(series_name)
    if not isinstance(raw_series, dict):
        raise APIError("Alpha Vantage returned no FX time series for this pair and timeframe.")

    candles: list[dict[str, object]] = []
    try:
        for timestamp, raw_candle in raw_series.items():
            if not isinstance(timestamp, str) or not isinstance(raw_candle, dict):
                continue
            parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                parsed_time = parsed_time.replace(tzinfo=timezone.utc)
            parsed_time = parsed_time.astimezone(timezone.utc)
            candle = {
                "time": parsed_time.isoformat(),
                "open": float(raw_candle["1. open"]),
                "high": float(raw_candle["2. high"]),
                "low": float(raw_candle["3. low"]),
                "close": float(raw_candle["4. close"]),
            }
            values = [candle[key] for key in ("open", "high", "low", "close")]
            if not all(math.isfinite(value) and value > 0 for value in values):
                raise ValueError("non-finite or non-positive candle value")
            if candle["high"] < max(candle["open"], candle["close"]) or candle["low"] > min(candle["open"], candle["close"]) or candle["high"] < candle["low"]:
                raise ValueError("inconsistent OHLC candle")
            candles.append(candle)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise APIError("Alpha Vantage returned malformed FX candle data.") from error

    candles.sort(key=lambda candle: candle["time"])
    if timeframe == "4H":
        candles = aggregate_four_hour_candles(candles)
    if not candles:
        raise APIError("Alpha Vantage returned no valid FX candles.")

    metadata = payload.get("Meta Data")
    last_refreshed = ""
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if "Last Refreshed" in str(key) and isinstance(value, str):
                last_refreshed = value
                break
    if not last_refreshed:
        last_refreshed = str(candles[-1]["time"])
    return candles, last_refreshed


def aggregate_four_hour_candles(candles: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[int, list[dict[str, object]]] = {}
    four_hours = 4 * 60 * 60
    for candle in candles:
        timestamp = datetime.fromisoformat(str(candle["time"])).timestamp()
        bucket = int(timestamp // four_hours) * four_hours
        grouped.setdefault(bucket, []).append(candle)

    result: list[dict[str, object]] = []
    for bucket, group in sorted(grouped.items()):
        result.append({
            "time": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
            "open": group[0]["open"],
            "high": max(candle["high"] for candle in group),
            "low": min(candle["low"] for candle in group),
            "close": group[-1]["close"],
        })
    return result


def get_alpha_vantage_candles(from_symbol: str, to_symbol: str, timeframe: str) -> dict[str, object]:
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        raise APIError("Alpha Vantage is not configured. Add your key to the local .env file and restart the server.", 503)

    pair = f"{from_symbol}/{to_symbol}"
    cache_key = (pair, timeframe, api_key)
    now = time.monotonic()
    cached = CACHE.get(cache_key)
    if cached and cached[0] > now:
        response = dict(cached[1])
        response["cached"] = True
        return response
    with UPSTREAM_CALLS_LOCK:
        UPSTREAM_CALLS[:] = [call_time for call_time in UPSTREAM_CALLS if now - call_time < UPSTREAM_CALL_WINDOW_SECONDS]
        if len(UPSTREAM_CALLS) >= MAX_UPSTREAM_CALLS_PER_DAY:
            raise APIError(
                "The shared public data limit has been reached for this 24-hour window. "
                "Try cached data later or adjust the host's server-side request limit.",
                429,
            )
        UPSTREAM_CALLS.append(now)

    function_by_timeframe = {
        "1H": "FX_INTRADAY",
        "4H": "FX_INTRADAY",
        "1D": "FX_DAILY",
        "1W": "FX_WEEKLY",
    }
    params = {
        "function": function_by_timeframe[timeframe],
        "from_symbol": from_symbol,
        "to_symbol": to_symbol,
        "apikey": api_key,
    }
    if timeframe in {"1H", "4H"}:
        params["interval"] = "60min"
        params["outputsize"] = "compact"
    elif timeframe == "1D":
        params["outputsize"] = "compact"

    request = Request(
        f"{API_URL}?{urlencode(params)}",
        headers={"User-Agent": "PivotFX/1.0"},
    )
    try:
        with urlopen(request, timeout=15) as upstream:
            payload = json.loads(upstream.read().decode("utf-8"))
    except HTTPError as error:
        raise APIError(f"Alpha Vantage returned HTTP {error.code}.", 502) from error
    except (URLError, TimeoutError, OSError) as error:
        raise APIError("Could not reach Alpha Vantage. Check the server's internet connection and try again.", 502) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise APIError("Alpha Vantage returned an unreadable response.", 502) from error

    if not isinstance(payload, dict):
        raise APIError("Alpha Vantage returned an unexpected response.", 502)
    provider_error = payload.get("Error Message") or payload.get("Information") or payload.get("Note")
    if provider_error:
        message = str(provider_error)
        status = 429 if "rate limit" in message.lower() or "call frequency" in message.lower() else 502
        if "premium" in message.lower():
            message += " Alpha Vantage marks the intraday FX endpoint as premium."
        raise APIError(message, status)

    candles, last_refreshed = parse_time_series(payload, timeframe)
    response = {
        "provider": "Alpha Vantage",
        "pair": pair,
        "timeframe": timeframe,
        "candles": candles,
        "lastRefreshed": last_refreshed,
        "timezone": "UTC",
        "cached": False,
    }
    cache_seconds = 60 if timeframe in {"1H", "4H"} else 900
    CACHE[cache_key] = (now + cache_seconds, response)
    return response


class PivotFXHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        if request.path == "/api/status":
            self.send_json(200, {
                "provider": "Alpha Vantage",
                "configured": bool(os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()),
            })
            return
        if request.path == "/api/candles":
            self.handle_candles(parse_qs(request.query))
            return
        self.serve_static(request.path)

    def handle_candles(self, query: dict[str, list[str]]) -> None:
        from_symbol = query.get("from", [""])[0].upper()
        to_symbol = query.get("to", [""])[0].upper()
        timeframe = query.get("timeframe", [""])[0].upper()
        if (from_symbol, to_symbol) not in ALLOWED_PAIRS:
            self.send_json(400, {"error": "Choose one of the supported currency pairs."})
            return
        if timeframe not in TIMEFRAMES:
            self.send_json(400, {"error": "Choose a supported timeframe: 1H, 4H, 1D, or 1W."})
            return
        try:
            response = get_alpha_vantage_candles(from_symbol, to_symbol, timeframe)
        except APIError as error:
            self.send_json(error.status, {"error": str(error)})
            return
        self.send_json(200, response)

    def serve_static(self, request_path: str) -> None:
        filename = "index.html" if request_path in {"/", ""} else request_path.lstrip("/")
        if filename not in STATIC_FILES:
            self.send_error(404)
            return
        file_path = ROOT / filename
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.log_date_time_string()} {format % args}")


def main() -> None:
    load_local_environment()
    server = ThreadingHTTPServer((HOST, PORT), PivotFXHandler)
    display_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME", f"{HOST}:{PORT}")
    print(f"Pivot FX listening on {display_host}")
    print("Alpha Vantage key: configured" if os.environ.get("ALPHAVANTAGE_API_KEY") else "Alpha Vantage key: not configured (sample data will be shown)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Pivot FX.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
