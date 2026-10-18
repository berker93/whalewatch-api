# How to connect whalewatch-web to whalewatch-api

Step by step: run the API, make an account, point the frontend at the API with real auth (`VITE_DATA_SOURCE=api`, `VITE_AUTH_SOURCE=api`), and check that the session cookie flows. Then what changes for staging and production.

What each endpoint takes and returns, and the adapter code, is in [frontend-integration.md](frontend-integration.md). This file is only about wiring the two together.

---

## 0 · The one thing to understand first: the cookie

The session is an **httpOnly cookie** that the API sets on login (`ww_session`, `SameSite=Lax`). The frontend never sees or stores a token. That means:

1. **Every request must send cookies**: `credentials: 'include'` on the openapi-fetch client (step 6).
2. **The page and the API must be the same *site*.** Ports don't matter; host names do. `localhost:5173` → `localhost:8000` is same-site, and the cookie flows. But `app.localhost:5173` → `localhost:8000` is **cross-site** (browsers treat `app.localhost` as its own site), so the browser won't send a `SameSite=Lax` cookie. `127.0.0.1` and `localhost` are different sites too.
3. **The page's origin must be on the API's CORS list.** That list is also the list of sites allowed to write; anything else gets `403 forbidden_origin`.

In production all of this holds by design: `whalewatch.com`, `app.whalewatch.com` and `api.whalewatch.com` are one site, and the cookie is set on `.whalewatch.com` (§9). In dev you choose a setup that keeps it true (step 5).

---

## 1 · Prerequisites

- Docker running, and both repos side by side:
  ```
  whale watch/
    whalewatch-api/
    whalewatch-web/
  ```
- In `whalewatch-api`, a `.env` copied from `.env.example`. The defaults are right for local dev: `EMAIL_BACKEND=log`, `SIGNUPS_OPEN=false`, CORS allowing the frontend's dev servers.

## 2 · Start the API and migrate

```bash
cd whalewatch-api
make up          # db, redis, api on http://localhost:8000 (hot reload)
make migrate     # applies 0034_accounts: app_user, user_session, account_token, signup_request, watchlist_item
```

No image rebuild is needed: the account code uses only the standard library (password hashing is `hashlib.scrypt`).

Check it's up: `curl -s localhost:8000/health` should answer `{"status":"ok",…}`.

## 3 · Make an account

Signups are closed by default (`SIGNUPS_OPEN=false`, matching the frontend's `FEATURES.signupsOpen`), so registering only records a request. Make an account by hand:

```bash
make cli c="create-user --email you@example.com --name 'Your Name'"
# prompts for the password twice (10+ characters), hidden
```

The default zone is `America/New_York`; pass `--time-zone Europe/London` for another. For scripts, pipe the password and add `--password-stdin`.

> To test the open-signup flow instead, set `SIGNUPS_OPEN=true` in `.env` and run `make up` (compose re-reads `.env`).

## 4 · Check the API on its own

From a terminal, with a cookie jar standing in for the browser:

```bash
J=/tmp/ww-cookies; O='Origin: http://localhost:5173'; H='content-type: application/json'

curl -s -c $J -H "$O" -H "$H" -d '{"email":"you@example.com","password":"<yours>"}' localhost:8000/v1/auth/login
curl -s -b $J localhost:8000/v1/auth/session        # {"session":{"user":{"email":"you@example.com"},…}}
curl -s -b $J localhost:8000/v1/account             # your profile
curl -s -b $J -X PUT -H "$O" -H "$H" -d '{"ref":{"kind":"investor","slug":"berkshire-hathaway"}}' \
  localhost:8000/v1/watchlist/items
curl -s "localhost:8000/v1/timeline?ref=investor:berkshire-hathaway&from=2026-06-21&to=2026-10-18&tz=America/New_York"
curl -s "localhost:8000/v1/timeline/upcoming?ref=investor:berkshire-hathaway&tz=America/New_York"
```

The interactive docs at <http://localhost:8000/docs> work too, including login, since the API accepts writes from its own origin.

## 5 · Choose a dev setup that keeps the cookie same-site

The frontend serves both surfaces from one dev server: marketing on `localhost:5173`, the app on `app.localhost:5173`. Because of §0.2, use one of these two setups.

### Option A: one origin for both surfaces (recommended)

Run both surfaces on `localhost:5173` and pick the surface with `?surface=app`, as the frontend already supports for Safari. Everything is then `localhost`, so login on either surface sets a cookie both surfaces send.

`whalewatch-web/.env.local`:

```bash
VITE_DATA_SOURCE=api
VITE_AUTH_SOURCE=api
VITE_API_URL=http://localhost:8000
VITE_MARKETING_ORIGIN=http://localhost:5173
VITE_APP_ORIGIN=http://localhost:5173
```

Open the app at <http://localhost:5173/?surface=app>; the choice is pinned to the tab.

Set the API's email links to the same origin, in `whalewatch-api/.env`:

```bash
WEB_URL=http://localhost:5173
```

The default, `http://app.localhost:5173`, is the frontend's default app origin, which Option A doesn't use. The confirm and reset links need no session, so they work from either origin, but the page has to exist where the link points.

### Option B: keep `app.localhost`, proxy the API through Vite

The browser then talks only to `app.localhost:5173`, and Vite forwards `/v1` to the API, so the cookie is first-party. Add to `vite.config.ts` (dev only):

```ts
server: {
  port: 5173,
  strictPort: true,
  proxy: { '/v1': 'http://localhost:8000' },
},
```

and in `.env.local` set `VITE_API_URL=` (empty, so requests are relative). Limitation: the cookie belongs to whichever host the login happened on, so the marketing surface on `localhost` won't see a session made on `app.localhost`. Fine if you sign in on the app surface; otherwise use Option A.

> Don't mix `127.0.0.1` and `localhost` anywhere in the chain. They are different sites.

## 6 · Regenerate the frontend's types and wire the client

```bash
cd whalewatch-api && make openapi          # only if you changed the API; openapi.json is committed
cd ../whalewatch-web && npm run gen:api    # reads ../whalewatch-api/openapi.json → src/api/schema.d.ts
```

Then in `whalewatch-web`:

1. **`src/api/client.ts`**: add `credentials: 'include'` to `createClient({...})`.
2. **`src/types/api.ts`**: add aliases for the new schemas (the checklist in frontend-integration.md §8).
3. **`src/api/source/apiSource.ts`**: implement `getAccount`, `updateAccount`, `getWatchlist`, `toggleWatch`, `getTimeline` and `getUpcomingFilings` (frontend-integration.md §3–4).
4. **`src/modules/Auth/internal/apiAdapter.ts`**: implement every method (frontend-integration.md §5).
5. Type-check: `npm run build` (or `npx tsc -b`). Every path, parameter and body is checked against the generated schema, so a mismatch is a compile error.

## 7 · Run both and check in the browser

```bash
cd whalewatch-web && npm run dev
```

1. Open <http://localhost:5173/?surface=app> (Option A) and log in with the account from step 3.
2. In DevTools → **Network**, the `POST /v1/auth/login` response should have `Set-Cookie: ww_session=…; HttpOnly; SameSite=lax`. Its request should carry `Origin: http://localhost:5173`, and the response `Access-Control-Allow-Credentials: true`.
3. In DevTools → **Application → Cookies → http://localhost:8000**, `ww_session` should be listed.
4. Later requests (`GET /v1/account`, `GET /v1/watchlist`) should carry `Cookie: ww_session=…` and answer 200.
5. Star an investor or a stock, then open `/timeline`: it should load from `GET /v1/timeline` and `GET /v1/timeline/upcoming`.
6. Account page: edit the name and time zone, then change the password. Other sessions are signed out.

### Reading the account emails in dev

With `EMAIL_BACKEND=log`, nothing is sent. Each email, link included, is in the API's log:

```bash
make logs s=api | grep mail.logged
```

Open the `…/account/confirm-email?token=…` or `…/reset-password?token=…` link from there. Until the frontend has those two pages (frontend-integration.md §6), confirm by hand:

```bash
curl -s -X POST -H 'Origin: http://localhost:5173' -H 'content-type: application/json' \
  -d '{"token":"<token>"}' localhost:8000/v1/account/email/confirm
```

## 8 · Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Login answers 200 but the next call is `401 unauthenticated` | The cookie isn't being sent | `credentials: 'include'` on the client; page and API on the same site (§0, step 5) |
| No `ww_session` under the API's cookies after login | Same, or the browser blocked a cross-site cookie | Use Option A or B; never `app.localhost` → `localhost:8000` directly |
| Console: "CORS policy: … Access-Control-Allow-Origin" | The page's origin isn't in `CORS_ORIGINS` | Unset means the dev servers (`localhost`, `127.0.0.1`, `app.localhost` on 5173 and 4173). Otherwise add the exact origin, no trailing slash |
| `403 forbidden_origin` on a write | Same, for writes | As above |
| `401 session_expired` | The session ended (12 h without "remember me", 30 days with), was signed out, or the password changed elsewhere | Log in again. This is the "Your session ended" path |
| `403 invalid_credentials` | Wrong email or password | The email is case-insensitive and trimmed; the password isn't |
| `429 rate_limited` on login | 10 auth calls per minute per IP | Wait `Retry-After` seconds, or raise `AUTH_RATE_LIMIT_PER_MINUTE` in dev |
| `422` on `/v1/timeline` | Window over 120 days, `from > to`, bad `tz`, or a `ref` not written `kind:id` | `detail.errors[].loc` names the parameter |
| Timeline has no Form 4 lines | None loaded for the window | `make cli c="crawl-form4 --catch-up"` then `make cli c="ingest-form4"` |
| `register` answers `{"status":"recorded"}` | Signups are closed | Expected. `create-user`, or `SIGNUPS_OPEN=true` |
| Types don't match after an API change | Stale `schema.d.ts` | `make openapi` in the API, then `npm run gen:api` |

## 9 · Staging and production

**API environment** (it refuses to boot without the first three outside dev):

```bash
ENVIRONMENT=production
CORS_ORIGINS=https://whalewatch.com,https://app.whalewatch.com
WEB_URL=https://app.whalewatch.com
EMAIL_BACKEND=smtp
SMTP_HOST=…  SMTP_PORT=587  SMTP_USERNAME=…  SMTP_PASSWORD=…  EMAIL_FROM='WhaleWatch <no-reply@whalewatch.com>'
SESSION_COOKIE_DOMAIN=.whalewatch.com     # one session for whalewatch.com and app.whalewatch.com
SIGNUPS_OPEN=false                        # flip together with FEATURES.signupsOpen in the frontend
```

- **Serve the API on a subdomain of the same site**, for example `https://api.whalewatch.com`. A different registrable domain (`whalewatch-api.fly.dev`) makes the cookie third-party, and browsers block it.
- **HTTPS everywhere.** The cookie is `Secure` in staging and production, so it is never sent over plain HTTP.
- **Behind a load balancer**, run uvicorn with `--proxy-headers --forwarded-allow-ips=<balancer>`. Otherwise both rate limits count every user as the balancer's IP.

**Frontend builds** (one per surface):

```bash
VITE_DATA_SOURCE=api
VITE_AUTH_SOURCE=api
VITE_API_URL=https://api.whalewatch.com
VITE_MARKETING_ORIGIN=https://whalewatch.com
VITE_APP_ORIGIN=https://app.whalewatch.com
VITE_SURFACE=marketing   # or app
```

**Order of a release that changes the contract:** deploy the API and its migration first, since the new endpoints are additive and old frontends keep working. Then regenerate types and deploy the frontend.
