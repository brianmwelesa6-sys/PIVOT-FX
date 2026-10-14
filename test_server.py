import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import server


def candle_payload(series_name, rows):
    return {
        "Meta Data": {"5. Last Refreshed": "2026-10-13"},
        series_name: rows,
    }


class ForexParsingTests(unittest.TestCase):
    def test_daily_series_is_sorted_and_uses_utc_timestamps(self):
        payload = candle_payload("Time Series FX (Daily)", {
            "2026-10-13": {"1. open": "1.10", "2. high": "1.12", "3. low": "1.09", "4. close": "1.11"},
            "2026-10-12": {"1. open": "1.08", "2. high": "1.11", "3. low": "1.07", "4. close": "1.10"},
        })

        candles, refreshed = server.parse_time_series(payload, "1D")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["time"], "2026-10-12T00:00:00+00:00")
        self.assertEqual(candles[-1]["close"], 1.11)
        self.assertEqual(refreshed, "2026-10-13")

    def test_four_hour_series_aggregates_hourly_ohlc(self):
        rows = {
            f"2026-10-13 {hour:02d}:00:00": {
                "1. open": str(1.10 + hour / 1000),
                "2. high": str(1.20 + hour / 1000),
                "3. low": str(1.00 + hour / 1000),
                "4. close": str(1.15 + hour / 1000),
            }
            for hour in range(8)
        }

        candles, _ = server.parse_time_series(candle_payload("Time Series FX (60min)", rows), "4H")

        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["open"], 1.10)
        self.assertAlmostEqual(candles[0]["high"], 1.203)
        self.assertAlmostEqual(candles[0]["low"], 1.0)
        self.assertAlmostEqual(candles[0]["close"], 1.153)
        self.assertEqual(candles[1]["time"], "2026-10-13T04:00:00+00:00")

    def test_rejects_invalid_ohlc_values(self):
        payload = candle_payload("Time Series FX (Daily)", {
            "2026-10-13": {"1. open": "1.10", "2. high": "1.09", "3. low": "1.08", "4. close": "1.11"},
        })
        with self.assertRaises(server.APIError):
            server.parse_time_series(payload, "1D")


class AlphaVantageProxyTests(unittest.TestCase):
    def test_key_is_required_before_upstream_request(self):
        with patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": ""}), patch("server.urlopen") as open_url:
            with self.assertRaises(server.APIError) as error:
                server.get_alpha_vantage_candles("EUR", "USD", "1D")

        self.assertEqual(error.exception.status, 503)
        open_url.assert_not_called()

    def test_public_daily_upstream_limit_prevents_additional_provider_calls(self):
        server.CACHE.clear()
        server.UPSTREAM_CALLS.clear()
        server.UPSTREAM_CALLS.extend([time_value for time_value in [100.0] * server.MAX_UPSTREAM_CALLS_PER_DAY])
        with patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": "test-key"}), patch("server.time.monotonic", return_value=101.0), patch("server.urlopen") as open_url:
            with self.assertRaises(server.APIError) as error:
                server.get_alpha_vantage_candles("EUR", "USD", "1D")

        self.assertEqual(error.exception.status, 429)
        open_url.assert_not_called()
        server.UPSTREAM_CALLS.clear()

    def test_daily_request_uses_documented_function_and_keeps_response_key_free(self):
        server.CACHE.clear()
        payload = candle_payload("Time Series FX (Daily)", {
            "2026-10-13": {"1. open": "1.10", "2. high": "1.12", "3. low": "1.09", "4. close": "1.11"},
        })
        response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with patch.dict(os.environ, {"ALPHAVANTAGE_API_KEY": "test-key"}), patch("server.urlopen", return_value=response) as open_url:
            result = server.get_alpha_vantage_candles("EUR", "USD", "1D")

        request = open_url.call_args.args[0]
        params = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(params["function"], ["FX_DAILY"])
        self.assertEqual(params["from_symbol"], ["EUR"])
        self.assertEqual(params["to_symbol"], ["USD"])
        self.assertNotIn("test-key", json.dumps(result))
        self.assertEqual(result["provider"], "Alpha Vantage")
        server.CACHE.clear()


if __name__ == "__main__":
    unittest.main()
