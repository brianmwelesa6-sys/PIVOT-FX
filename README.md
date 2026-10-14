# Pivot FX

Pivot FX is a local forex analysis dashboard with an Alpha Vantage server-side data adapter. It supports six major pairs, candlestick charts, EMA(20/50), RSI(14), ATR(14), and 1-hour, 4-hour, daily, and weekly timeframes. The trading workspace can validate, preview, and retain order drafts in the current browser session.

## Trading workspace status

The trade ticket is **not connected to a broker**. The order button is intentionally disabled; the site cannot submit orders, access account balances or positions, or move funds. Preview prices are candle references (or clearly labelled sample values), not executable quotes. Local drafts are temporary UI state and are not persisted or sent anywhere. Do not treat the ticket as a real or paper brokerage account.

Live trading would require choosing a regulated broker that serves your jurisdiction, reviewing its official API and account permissions, implementing its authentication and order lifecycle safely on a server, and testing in its sandbox. Alpha Vantage provides market-data APIs, not brokerage execution. No live order support is implemented here.

## Selected broker: Pepperstone cTrader

Pepperstone Markets Limited is shown as the selected broker, but no account is connected and the site has not verified which Pepperstone legal entity or account type applies to you. Confirm directly with Pepperstone that your account is eligible for cTrader and cTrader Open API.

The documented cTrader Open API integration uses OAuth consent and a registered application, not your Pepperstone password. Before implementing the connection:

1. Register an application in the [cTrader Open API portal](https://openapi.ctrader.com/apps) and obtain its Client ID and Client Secret.
2. Configure the app's redirect URI to match the local app callback; do not put the Client Secret in browser code or send it in chat.
3. Add application credentials to the ignored local `.env` file only after the callback URL and server-side OAuth implementation have been prepared.
4. Authorize a **demo** cTrader account first. cTrader distinguishes the read-only `accounts` OAuth scope from the `trading` scope, which permits trading operations. Live-account access should remain disabled until the OAuth, account selection, order preview, explicit per-order consent, and order/position reconciliation have been tested.

Official references: [cTrader Open API overview](https://help.ctrader.com/open-api/), [OAuth authentication and scopes](https://help.ctrader.com/open-api/account-authentication/), [cTrader Open API terms](https://help.ctrader.com/open-api/terms-of-use/), and [Pepperstone platform information](https://pepperstone.com/en/platforms/). The cTrader terms require explicit prior user approval for trading actions. No cTrader OAuth flow, account access, or order execution is implemented yet.

## Start the website

1. Get an API key from [Alpha Vantage](https://www.alphavantage.co/support/#api-key).
2. Copy `.env.example` to `.env` in this project folder, open `.env` in a text editor, and replace the placeholder with your key:

   ```text
   ALPHAVANTAGE_API_KEY=your_actual_key
   ```

3. From this project folder, start the local server:

   ```powershell
   py server.py
   ```

4. Open <http://127.0.0.1:8000>.

The server reads the key from `.env` (or the `ALPHAVANTAGE_API_KEY` environment variable) and makes Alpha Vantage requests itself. The browser never receives the API key. `.env` is excluded by `.gitignore`; do not share or commit it. Without a configured key, the dashboard remains available with clearly labelled illustrative sample data.

## Publish a public Android-accessible link

`render.yaml` prepares a public Render web service. To publish it, push the project to a GitHub repository **without the local `.env` file**, then in Render choose **New → Blueprint**, connect that repository, and provide `ALPHAVANTAGE_API_KEY` when Render requests the secret value. Render builds the service and provides a public `https://…onrender.com` URL that Android can open. The server binds to Render's assigned port and exposes `/healthz` for health checks.

The public site is open to anyone with its URL, as requested. Its Alpha Vantage key remains in Render's server-side environment settings and is never included in the repository or sent to browsers. To reduce accidental provider-quota exhaustion, the server limits uncached provider requests to 20 per rolling 24-hour period per running instance; cached chart responses do not count. This limit is shared by all visitors and resets when the service restarts. Public visitors can still use the site's read-only data API, so rotate the Alpha Vantage key if it is ever exposed and monitor its usage in the Alpha Vantage dashboard.

Do not upload `.env`, publish API credentials in a source repository, or enable broker credentials. Pepperstone/cTrader remains disconnected and all live orders remain disabled.

## Alpha Vantage endpoints

The adapter uses the official [Alpha Vantage Forex documentation](https://www.alphavantage.co/documentation/#fx):

- `FX_DAILY` for daily candles and `FX_WEEKLY` for weekly candles.
- `FX_INTRADAY` at `60min` for hourly candles. The 4-hour view aggregates those hourly bars into UTC four-hour candles.

Alpha Vantage documents the FX intraday endpoint as premium. Hourly and 4-hour views therefore require an API plan with intraday access; daily and weekly views use the corresponding FX endpoints. The default view is daily. The server uses compact intraday/daily responses and caches successful responses briefly to reduce repeated requests. Provider errors, quota messages, and missing credentials are surfaced in the dashboard rather than disguised as live data.

## Data and analysis

The watchlist marks unrequested pair prices as samples; selecting a pair loads its candles through the server. Chart indicators are calculated from the candles currently displayed. Quotes can be delayed and are subject to the provider's account, licensing, and request limits. Technical indicators are descriptive calculations, not predictions or financial advice.

## API routes

- `GET /api/status` reports whether the local server has a key configured (never returns the key).
- `GET /api/candles?from=EUR&to=USD&timeframe=1D` returns validated OHLC candles for a supported pair and timeframe.
