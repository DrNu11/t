# Debug Session: cors-websocket

Status: [OPEN]

## Symptoms

- Browser reports hydration attributes `trancy-version` and `crxlauncher`.
- Requests from `http://localhost:3002` to the API on port 8000 are blocked by CORS despite HTTP 200 responses.
- Binance WebSocket connection closes before establishment.

## Hypotheses

1. The API process has not been restarted since the CORS configuration changed.
2. `backend/.env` overrides `CORS_ALLOW_ORIGINS` and excludes port 3002.
3. Port 8000 is served by a stale or unrelated process.
4. Binance port 9443 is unreachable from the current network.
5. Hydration attributes are injected by browser extensions rather than project code.

## Evidence

- `GET /api/events` returns HTTP 200 from uvicorn but has no `Access-Control-Allow-Origin` for `http://localhost:3002`.
- The `OPTIONS /api/agent_chat` preflight returns HTTP 400 with no CORS allow-origin header.
- Port 8000 is owned by process 13296.
- `backend/.env` does not override `CORS_ALLOW_ORIGINS`.
- Direct TCP tests to `stream.binance.com` fail on both ports 9443 and 443.
- No project source contains `trancy-version` or `crxlauncher`.

## Analysis

- Hypothesis 1 is confirmed: the running API process is using stale CORS configuration and must be restarted.
- Hypothesis 2 is rejected: no environment override exists.
- Hypothesis 4 is confirmed: Binance is unreachable from the current network, independent of WebSocket parsing.
- Hypothesis 5 is confirmed: hydration attributes come from browser extensions.

## Fix

- Stopped the stale API process on port 8000.
- Installed the backend dependencies declared in `backend/requirements.txt` because the active Python 3.14 environment lacked `aiosqlite`.
- Restarted the current API server on `127.0.0.1:8000`.

## Post-fix Evidence

- `GET /api/events` returns HTTP 200 with `Access-Control-Allow-Origin: http://localhost:3002`.
- `OPTIONS /api/agent_chat` returns HTTP 200 with the same allow-origin header and permits POST.
- Binance remains unreachable at the TCP/network level on both 9443 and 443; no code-only WebSocket endpoint change can bypass this network restriction.

## Follow-up Evidence

- React development Strict Mode immediately unmounts the first effect, closing the WebSocket while it is still connecting (line 64 warning).
- Subsequent line 24 failures match the independently confirmed Binance network block.
- The hydration warning names only the two extension-injected attributes already absent from project source.

## Follow-up Fix

- Added `suppressHydrationWarning` to the root `<html>` element for extension-injected attributes.
- Deferred the initial WebSocket connection by one task so React Strict Mode can cancel its development-only first effect before a socket is created.
- Frontend production build completes successfully after both changes.

## Development Cache Recovery

Running `next build` while `next dev` was active caused both processes to overwrite the shared `.next` directory. This reproduced missing development chunks and HTTP 500 responses. The development process was stopped, `.next` was deleted, and `next dev --port 3002` was restarted. Post-restart verification returned HTTP 200 for the page, layout CSS, webpack runtime, main app, app internals, page chunk, and polyfills.

## Binance Endpoint Recovery

The official market-data-only endpoint `data-stream.binance.vision:443` passed TCP connectivity and a real WebSocket handshake, returning a combined SOL ticker frame. The frontend endpoint was changed from the blocked `stream.binance.com:9443` host to this verified endpoint without changing ticker semantics or parsing.
