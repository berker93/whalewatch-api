# Frontend integration — Epic 9 endpoints (Timeline + Account)

How whalewatch-web replaces the `501` / `unavailable` stubs in `apiSource` and `apiAuth` with the endpoints that shipped for `docs/be-requirements-timeline-account.md` (BE-1 to BE-5, BE-7, BE-8, plus the auth the epic listed as a prerequisite).

- **Who this is for:** whoever implements `src/api/source/apiSource.ts` and `src/modules/Auth/internal/apiAdapter.ts`.
- **Setting up the two repos:** [how-to-connect-frontend.md](how-to-connect-frontend.md) covers env vars, cookies, CORS and regenerating types.
- **The contract:** `openapi.json` at this repo's root. `npm run gen:api` turns it into `src/api/schema.d.ts`. Every shape below is in it, with field descriptions.

---

## 1 · What shipped

| FE seam | Endpoint | Req | Notes |
|---|---|---|---|
| `AuthAdapter.login` | `POST /v1/auth/login` | BE prereq | Sets the session cookie |
| `AuthAdapter.logout` | `POST /v1/auth/logout` | BE prereq | `204`, idempotent |
| `AuthAdapter.getSession` | `GET /v1/auth/session` | BE prereq | `{session: null}` when signed out |
| `AuthAdapter.register` | `POST /v1/auth/register` | BE prereq | `202`: `recorded` while signups are closed, `check_email` once open. No cookie either way |
| `AuthAdapter.requestPasswordReset` | `POST /v1/auth/password-reset` | BE prereq | `204` whether or not the address has an account |
| *(new, see §6)* | `POST /v1/auth/password-reset/confirm` | BE prereq | Target of the reset link |
| `AuthAdapter.changePassword` | `POST /v1/auth/password` | BE-3 | `204`. Signs out every other session |
| `getAccount` | `GET /v1/account` | BE-1 | Now also carries `pending_email` |
| `updateAccount` | `PATCH /v1/account` | BE-1, BE-2 | All or nothing |
| *(new, see §6)* | `POST /v1/account/email/confirm` | BE-2 | Target of the confirmation link. No session needed |
| `getWatchlist` | `GET /v1/watchlist` | BE-4 | Three lists, always present |
| `toggleWatch` | `PUT` / `DELETE /v1/watchlist/items` | BE-4 | Both answer with the whole watchlist |
| `getTimeline` | `GET /v1/timeline` | BE-7 | Option B. Not paged |
| `getUpcomingFilings` | `GET /v1/timeline/upcoming` | BE-8 | |
| *(optional)* | `GET /v1/insiders/transactions?insider_cik=&filed_from=&filed_to=` | BE-5 | Shipped too (Q5): link a followed insider to their own lines |

**Not shipped:** BE-6 (`accession_no` / `filed_at` on `Activity`, `traded_value_usd` on `StockOwner`). It was only needed for option A, and option B shipped.

### Where the API differs from the proposal

Read these before mapping. Each is a deliberate change, not an oversight.

1. **Refs travel as one `ref` parameter.** The proposal had separate `stock` / `investor` / `insider` params. Instead, each ref is `ref=<kind>:<id>`, repeated, which is exactly what `watchRefKey()` in `src/api/source/watchRef.ts` already writes: `ref=stock:037833100&ref=investor:berkshire-hathaway&ref=insider:0001197647`. A single parameter keeps the order refs were sent in, and `/upcoming` must answer in watchlist order.
2. **Timeline and upcoming answer in the usual envelope**, `{ data, meta, page: null }`, not as a bare array. `meta` echoes the window and the zone used.
3. **Event `kind`s are snake_case on the wire**: `insider_trade`, `holder_change`, `filing_summary`. The FE's are kebab-case. The mapping is in §4.
4. **`GET /v1/auth/session` answers `{ session: Session | null }`**, and `Session` has an `expires_at` beside `user`.
5. **`AccountProfile.pending_email` exists** (BE-2's AC), so a reload still shows "Confirmation link sent to …".
6. **Registration takes an optional `time_zone`, but don't send it.** Every new account starts on `America/New_York` (Q2, settled), and the user changes it on the account page.
7. **Registering never says whether an address has an account.** The answer is `202 { status: 'check_email' }` either way, with no cookie; the inbox hears which. There is no `email_taken` on register.

---

## 2 · Calling the API

### Credentials

Every call must send the session cookie. Set it once on the client in `src/api/client.ts`:

```ts
export const api = createClient<paths>({
  baseUrl: import.meta.env.VITE_API_URL ?? 'http://localhost:8000',
  credentials: 'include',
})
```

Without `credentials: 'include'` the browser drops the `Set-Cookie` from login and sends no cookie, and every signed-in route answers `401 unauthenticated`. The public endpoints don't care either way.

### Responses that are one person's

Auth, account, watchlist and saved-filter responses carry `Cache-Control: no-store`. The timeline isn't cached by the API either. Keep React Query's own caching as it is; just never persist these responses anywhere shared.

Every read of 13F or Form 4 data now depends on the caller's plan, so it answers `Cache-Control: private` with `Vary: Cookie`, not `public`. Two consequences for the page:

- **Key plan-dependent queries on the account too.** Free and Pro get different bodies for the same URL. After a sign-in, sign-out or plan change, invalidate the data queries, not just `me`.
- **History stops at the plan's window.** `history_quarters` counts back from the quarter being filed (on 25 October 2026, Free's 4 are 2025Q4 to 2026Q3), and `insider_months` from today. Lists and charts simply start there. A request that names an earlier `period`, `from` or `to` is a `402 plan_limit` (below).

### Errors

Errors keep the existing shape, `{ error: { code, message, detail, request_id } }`, so `SourceError` / `toProblem()` already read them. What's new:

| Status | `code` | Where | Meaning |
|---|---|---|---|
| 401 | `unauthenticated` | any signed-in route | The request carried no session cookie |
| 401 | `session_expired` | any signed-in route, `GET /v1/auth/session` | The cookie named a session that ended, ran out or was signed out. The response also clears the cookie |
| 403 | `invalid_credentials` | login, change password, delete account | Wrong email or password, or wrong current password. **Never a 401** |
| 403 | `forbidden_origin` | any write | The `Origin` isn't on the API's allow-list. A config error, not a user error |
| 403 | `email_unverified` | watchlist `PUT` / `DELETE`, saved-filter `POST` / `DELETE` | The account's email isn't verified yet: it reads everything and saves nothing. Offer "resend the link" (§5) |
| 402 | `plan_limit` | any 13F or Form 4 read, watchlist `PUT`, saved-filter `POST` | The plan doesn't reach that far back, or holds no more. `detail` is `{ limit, allowed, needed, plan }`: `limit` is the entitlement key (`history_quarters`, `insider_months`, `watchlist_items`, `screener_saved_filters`), and `plan` is the cheapest public plan that lifts it (`{ slug, name, price_cents, currency, interval, value }`), or `null` when none does. Show an upgrade prompt from it, not an error |
| 409 | `email_taken` | account email change | The new address already has an account. Never from register |
| 422 | `validation_error` | everywhere | `detail.errors[].loc` is `["body", "<wire field>"]` or `["query", "<param>"]` |
| 422 | `weak_password` / `invalid_email` / `invalid_name` | register, change password, reset | Same `detail.errors` shape, with its own code |
| 422 | `terms_not_accepted` | register | `accept_terms` wasn't `true`. `detail.errors[].loc` is `["body","accept_terms"]`: show it under the terms checkbox |
| 422 | `invalid_token` | email confirm, reset confirm | The link is unknown, used, replaced or expired |
| 429 | `rate_limited` | login, register, reset request, resend verification | 5 attempts per 15 minutes, per IP **and** per email, each action on its own. Every attempt counts, successful ones too. `Retry-After` is set |
| 429 | `rate_limited` | change password, the emailed links' confirm calls | 10 per minute per IP. `Retry-After` is set |
| 503 | `unavailable` | any signed-in route, login | Sessions can't be read right now (Redis is down). **Not** a sign-out: keep the user where they are and retry after `Retry-After` |

`isUnauthorized()` (status 401) stays correct: a 401 always means "send them to log in".

---

## 3 · Account (BE-1, BE-2)

### Shapes

| Wire (`AccountProfile`) | FE (`AccountProfile`) |
|---|---|
| `name` | `name` |
| `email` | `email` |
| `time_zone` | `timeZone` |
| `member_since` | `memberSince` |
| `password_changed_at` | `passwordChangedAt` |
| `pending_email` | *(not on the FE type yet. Add it as `pendingEmail` to drop the TA-11 reload gap)* |

`PATCH` body (`AccountPatch`): `name?`, `email?`, `time_zone?`. A field left out, or `null`, stays as it is. Unknown fields are a 422, so send `time_zone`, not `timeZone`.

`PATCH` answer (`AccountUpdate`): `{ profile: AccountProfile, pending_email: string | null }`.

### Behaviour the page relies on

- **All or nothing.** Every field is checked before any is applied, and every refusal comes back in one 422, one `detail.errors` entry per field: `["body","name"]`, `["body","email"]`, `["body","time_zone"]`. `refusedFields()` already reads these.
- **An email on another account is a 422 on `email`**, not a 409, as TA-11 asked.
- **Rules are the FE's or looser:** name ≥ 2 characters once trimmed (≤ 200), email matches `EMAIL_PATTERN` (≤ 254), any IANA zone Postgres knows. `name` is stored trimmed; `email` is stored as typed and compared without case (`Ada@` and `ada@` are one account). A change of case alone is no change.
- **A new email waits.** `profile.email` keeps the old one, `pending_email` names the new one, a link goes to the new address and a notice to the old. Sending the current email again is "no change" and cancels a waiting change. A second change replaces the first, and the first link stops working.
- **Confirming changes the login.** After `POST /v1/account/email/confirm`, `GET /v1/auth/session` reports the new address and the old one no longer logs in. It also verifies the account's email: the link reached the new address.

### `apiSource`

```ts
// src/api/source/apiSource.ts
import type { AccountProfile } from '@/types'
import type { AccountProfile as WireProfile } from '@/types/api'   // alias it in types/api.ts

const toProfile = (p: WireProfile): AccountProfile => ({
  name: p.name,
  email: p.email,
  timeZone: p.time_zone,
  memberSince: p.member_since,
  passwordChangedAt: p.password_changed_at,
})

getAccount: async () => toProfile(await unwrap(api.GET('/v1/account'))),

updateAccount: async (patch) => {
  const update = await unwrap(
    api.PATCH('/v1/account', {
      body: { name: patch.name, email: patch.email, time_zone: patch.timeZone },
    }),
  )
  return { profile: toProfile(update.profile), pendingEmail: update.pending_email ?? null }
},
```

`undefined` fields drop out of the JSON, which is what "omitted stays as it is" needs. TA-10's one-field-per-save PATCH works unchanged.

---

## 4 · Watchlist and timeline (BE-4, BE-7, BE-8)

### Refs

| FE `WatchRef` | Wire body (`WatchRef`) | Query key (`ref=`) |
|---|---|---|
| `{ kind: 'stock', cusip }` | `{ kind: 'stock', cusip }` | `stock:<cusip>` |
| `{ kind: 'investor', slug }` | `{ kind: 'investor', slug }` | `investor:<slug>` |
| `{ kind: 'insider', insiderCik }` | `{ kind: 'insider', insider_cik }` | `insider:<cik>` |

The API accepts a CUSIP in any case and a CIK with or without its leading zeros, and always answers with uppercase CUSIPs and ten-digit CIKs.

```ts
// src/api/source/watchRef.ts (or apiSource.ts)
import type { WatchRef as WireRef } from '@/types/api'

export const toWireRef = (ref: WatchRef): WireRef =>
  ref.kind === 'insider' ? { kind: 'insider', insider_cik: ref.insiderCik } : ref

export const fromWireRef = <R extends WireRef>(ref: R) =>
  (ref.kind === 'insider' ? { kind: 'insider', insiderCik: ref.insider_cik } : ref) as WatchRef
```

### Watchlist

- `GET /v1/watchlist` → `{ securities: StockMatch[], investors: InvestorMatch[], insiders: InsiderMatch[] }`. The shape is the FE's `Watchlist` exactly, and `InsiderMatch` has the six fields TA-2 picked. Each list is in the order items were added, oldest first. Nothing followed is `200` with three `[]`.
- `PUT /v1/watchlist/items` `{ ref }` follows; `DELETE /v1/watchlist/items` `{ ref }` unfollows. Both are idempotent and answer with the whole watchlist. A ref that names nothing is a `404` either way. A malformed CUSIP or CIK is a `422` with `loc: ["body","ref","cusip"]`. Following past the plan's `watchlist_items` (10 on Free) is a `402 plan_limit`; following something already followed is never refused. No plan keeps more than 500, and the 501st is a `422`.

### Saved filters

`apiSource`'s `notYet('saved-filters')` can go:

- `GET /v1/saved-filters` → `{ data: SavedFilter[] }`, oldest first. `SavedFilter` is `{ id, name, query, created_at }`: `id` is a string, as the FE type has it, and `query` is `GET /v1/flows`'s parameters with the ones left at their defaults left out. A period comes back as `2026Q1` however it was sent.
- `POST /v1/saved-filters` `{ name, query }` → `201` with the `SavedFilter`. `query` takes `period`, `period_type`, `min_investors`, `min_value`, `direction`, `sort`, `order`; anything else (`sector`, paging) is a `422`. The plan's `screener_saved_filters` (3 on Free) more is a `402 plan_limit`. Verified email only, as for the watchlist.
- `DELETE /v1/saved-filters/{id}` → `204`, idempotent.

`toggleWatch` has to know whether the ref is held. Read it from the watchlist the app already has in the query cache, or fetch it:

```ts
const holds = (list: Watchlist, ref: WatchRef) =>
  ref.kind === 'stock' ? list.securities.some((s) => s.cusip === ref.cusip)
  : ref.kind === 'investor' ? list.investors.some((i) => i.slug === ref.slug)
  : list.insiders.some((i) => i.insider_cik === ref.insiderCik.padStart(10, '0'))

getWatchlist: () => unwrap(api.GET('/v1/watchlist')),

toggleWatch: async (ref) => {
  const body = { ref: toWireRef(ref) }
  const current = await unwrap(api.GET('/v1/watchlist'))
  return unwrap(
    holds(current, ref)
      ? api.DELETE('/v1/watchlist/items', { body })
      : api.PUT('/v1/watchlist/items', { body }),
  )
},
```

**First-sign-in migration** (BE-4 AC): one `PUT` per browser-only item that has a CUSIP, slug or CIK, before the first `GET` that `/` acts on. A `404` for one item (a CUSIP the API doesn't know) shouldn't stop the rest; keep that item in the browser, as TA-2 does for unresolved tickers.

### Timeline

`GET /v1/timeline?ref=…&from=YYYY-MM-DD&to=YYYY-MM-DD&tz=<IANA>`

- `from` / `to` are days in `tz`, both included, at most 120 days apart counting both. A window under 14 days is answered as asked.
- `tz` should always be sent (TA-4). If it's left out, the API uses the signed-in user's zone, or `America/New_York` for no one.
- **Not paged.** The whole window comes back in one answer, newest first, ties by `id`, so `fetchAllPages` isn't needed.
- 422 when a date isn't real, `from > to`, the window is over 120 days, `tz` isn't an IANA zone, or a `ref` isn't `kind:id`. Each names its `["query", "<param>"]`.

The rules are TA-3's, served: `buildTimeline()` stays the mock and the oracle, and `tests/integration/test_timeline_endpoint.py` asserts its cases against the API.

Event mapping, wire → FE:

| Wire | FE | Kinds |
|---|---|---|
| `kind: insider_trade` / `holder_change` / `filing_summary` | `kind: 'insider-trade'` / `'holder-change'` / `'filing-summary'` | |
| `id`, `at`, `side`, `source` | same | all |
| `value_usd` | `value` | all. Signed decimal string, or `null` |
| `ref` | `ref` (via `fromWireRef`) | all |
| `trade` | `trade` | insider_trade. A full `InsiderTransaction` |
| `period`, `investor` | same | 13F kinds |
| `as_of` | `asOf` | 13F kinds |
| `lag_days` | `lagDays` | 13F kinds |
| `accession_no` | `accessionNo` | 13F kinds |
| `change` | `change` | holder_change. The eight fields of the FE's `Pick<Activity,…>` |
| `counts` | `counts` | filing_summary |
| `largest_change` | `largestChange` | filing_summary. Unsigned `traded_value_usd`, as TA-6 expects |
| `position_count` | `positionCount` | filing_summary |
| `portfolio_value_usd` | `aumUsd` | filing_summary |

```ts
import type { TimelineEvent as WireEvent } from '@/types/api'

function toTimelineEvent(e: WireEvent): TimelineEvent {
  const base = { id: e.id, at: e.at, side: e.side, value: e.value_usd ?? null }
  if (e.kind === 'insider_trade') {
    return { ...base, kind: 'insider-trade', source: 'form4', ref: fromWireRef(e.ref), trade: e.trade } as TimelineEvent
  }
  const holdings = {
    source: '13f' as const,
    period: e.period,
    asOf: e.as_of,
    lagDays: e.lag_days,
    accessionNo: e.accession_no,
    investor: e.investor,
  }
  return e.kind === 'holder_change'
    ? ({ ...base, ...holdings, kind: 'holder-change', ref: e.ref, change: e.change } as TimelineEvent)
    : ({
        ...base,
        ...holdings,
        kind: 'filing-summary',
        ref: e.ref,
        counts: e.counts,
        largestChange: e.largest_change ?? null,
        positionCount: e.position_count,
        aumUsd: e.portfolio_value_usd ?? null,
      } as TimelineEvent)
}

getTimeline: async ({ refs, from, to, timeZone }) => {
  const { data } = await unwrap(
    api.GET('/v1/timeline', {
      params: { query: { ref: refs.map(watchRefKey), from, to, tz: timeZone } },
    }),
  )
  return data.map(toTimelineEvent)
},
```

openapi-fetch's default query serializer writes arrays as repeated keys (`ref=a&ref=b`), which is what the API reads. Commas work too.

Details that differ from nothing in the FE, but are worth knowing:

- `at` is `filed_at`: EDGAR's acceptance time, in UTC, to the second. A Form 4 always has a clock time.
- **A Form 4 line with no linked security still matches a followed stock when its issuer filed it** (Q6). The loader leaves `cusip` null when a company has two share classes and the filing names neither or both (Lennar writes `LEN, LEN.B`). Such a P or S line comes under the followed stock of that company, with `ref.cusip` set and `trade.cusip === null`. Link the row by `ref`, or by `trade.issuer_ticker`, not by `trade.cusip`. If both classes are followed it's still one event, under the first one sent. `/v1/stocks/{ticker}/insiders` already treats these lines the same way.
- **`buildTimeline()` needs the same rule to stay the oracle.** Today it drops lines with a null `cusip`. Matching by issuer needs each followed stock's issuer CIK, which the mock has (`InsiderTransaction.issuer_cik`) but `StockMatch` doesn't. Matching on `issuer_ticker` containing the stock's ticker is close enough for the mock.
- A followed insider's lines come under `ref.kind = 'insider'` even when their stock is followed too: one event, never two.

### Upcoming

`GET /v1/timeline/upcoming?ref=…&tz=…` → `{ data: UpcomingFiling[], meta }`

| Wire | FE |
|---|---|
| `ref` | `ref` (stock or investor only. Insider refs are ignored) |
| `period` | `period` |
| `due_at` | `dueAt` |
| `pending_holders` (`null` for investors) | `pendingHolders` (`undefined` for investors) |

```ts
getUpcomingFilings: async ({ refs, timeZone }) => {
  const { data } = await unwrap(
    api.GET('/v1/timeline/upcoming', { params: { query: { ref: refs.map(watchRefKey), tz: timeZone } } }),
  )
  return data.map((u) => ({
    ref: u.ref,
    period: u.period,
    dueAt: u.due_at,
    pendingHolders: u.pending_holders ?? undefined,
  }))
},
```

The current quarter is the one that ended most recently in `tz`; `meta.period` and `meta.coverage` say which and how full it is. Items come in the order the refs were sent.

---

## 5 · Auth (`apiAuth`)

### Shapes

| Endpoint | Body | Answer |
|---|---|---|
| `POST /v1/auth/login` | `{ email, password, remember? }` (`remember` defaults to `true`) | `Session` + cookie |
| `POST /v1/auth/logout` | none | `204`, cookie cleared |
| `GET /v1/auth/session` | none | `{ session: Session \| null }`, or `401 session_expired` |
| `POST /v1/auth/register` | `{ name, email, password, time_zone?, accept_terms, marketing_email? }` | `202 { status: 'recorded' }` or `202 { status: 'check_email' }`. Never a cookie, and the same answer whether or not the address has an account |
| `POST /v1/auth/password` | `{ current_password, new_password }` | `204` |
| `POST /v1/auth/password-reset` | `{ email }` | `204`, always |
| `POST /v1/auth/password-reset/confirm` | `{ token, new_password }` | `204` |
| `POST /v1/auth/verify-email` | `{ token }` | `200 Session` + cookie: the link signs in. No session needed |
| `POST /v1/auth/verify-email/resend` | none, signed in | `204`. Nothing is sent once verified |
| `GET /v1/me` | none | `{ user, plan, entitlements, usage }` (below). Signed out: `200` with `user` and `usage` null, Free's plan and entitlements. `401 session_expired` only for a session that has ended |

`Session` is `{ user: { email, email_verified }, expires_at }`. The FE's `Session` only needs `user`.

`GET /v1/me` is the one call an app page needs before it renders: `user` (`email`, `name`, `time_zone`, `email_verified`, `email_verified_at`, `created_at`, `last_seen_at`), `plan` (`{ id, name }`: `free`, `pro`, `api_hobbyist` or `api_pro`; `free` without a subscription) and `entitlements`. Gate the follow buttons on `entitlements.save`, not on `email_verified`.

`entitlements` is every capability the account has, resolved: `save` (verified email), the limits `history_quarters`, `insider_months`, `watchlist_items`, `screener_saved_filters`, `api_calls_month` and `mcp_calls_month` (a whole number, or `null` for no limit), and the flags `export_enabled`, `alerts_instant` and `mcp_enabled`. They are rows in the database, changed without a release, so read them from here and never write a plan's numbers into the page. A change to a plan reaches a signed-in session within about a minute, and a change to one account's subscription within five. The API enforces `history_quarters`, `insider_months`, `watchlist_items` and `screener_saved_filters` (a `402 plan_limit` when one stops a request), and `api_calls_month` on requests made with an API key (§5b); `export_enabled`, `alerts_instant`, `mcp_calls_month` and `mcp_enabled` have no endpoint yet.

`remember: false` gives a cookie without `Max-Age`, which ends with the browser, and a server-side session of 12 hours. `remember: true` gives 30 days.

### Error mapping to `AuthErrorCode`

| API `code` | `AuthErrorCode` |
|---|---|
| `invalid_credentials`, `invalid_email`, `invalid_name`, `weak_password`, `session_expired` | same |
| `unauthenticated` (from `changePassword`) | `session_expired`. TA-12: a 401 reads as "Your session ended" |
| `email_unverified` | No FE code yet. Show a "verify your email" banner with a resend button rather than an error |
| `rate_limited`, `unavailable`, anything else, network failure | `unavailable` (TA-12: a `rate_limited` code can come later) |

### The adapter

```ts
// src/modules/Auth/internal/apiAdapter.ts
import { api } from '@/api/client'
import type { AuthErrorCode, Session } from '@/types'

import type { AuthAdapter } from '../adapter'
import { AuthError } from '../errors'

const PASSED_THROUGH = new Set<string>([
  'invalid_credentials', 'invalid_email', 'invalid_name', 'weak_password', 'session_expired',
])

function toAuthError(error: unknown): AuthError {
  const code = (error as { error?: { code?: string } } | undefined)?.error?.code ?? ''
  if (PASSED_THROUGH.has(code)) return new AuthError(code as AuthErrorCode)
  if (code === 'unauthenticated') return new AuthError('session_expired')
  return new AuthError('unavailable')
}

/** Like unwrap(), but 204s have no body and failures are AuthErrors. */
async function call<T>(request: Promise<{ data?: T; error?: unknown }>): Promise<T> {
  let result: { data?: T; error?: unknown }
  try {
    result = await request
  } catch {
    throw new AuthError('unavailable')
  }
  if (result.error !== undefined) throw toAuthError(result.error)
  return result.data as T
}

const toSession = (s: { user: { email: string } }): Session => ({ user: { email: s.user.email } })

export const apiAuth: AuthAdapter = {
  login: async (credentials, { remember = true } = {}) =>
    toSession(await call(api.POST('/v1/auth/login', { body: { ...credentials, remember } }))),

  register: async ({ name, email, password }) => {
    // No time_zone: every account starts on America/New_York (Q2).
    const result = await call(api.POST('/v1/auth/register', { body: { name, email, password, accept_terms, marketing_email } }))
    return { status: result.status } // 'recorded' or 'check_email': never a session
  },

  logout: async () => {
    await call(api.POST('/v1/auth/logout'))
  },

  getSession: async () => {
    const { session } = await call(api.GET('/v1/auth/session'))
    return session ? toSession(session) : null
  },

  requestPasswordReset: async (email) => {
    await call(api.POST('/v1/auth/password-reset', { body: { email } }))
  },

  changePassword: async ({ current, next }) => {
    await call(
      api.POST('/v1/auth/password', { body: { current_password: current, new_password: next } }),
    )
  },

  // The cookie is on the API's host. In production SESSION_COOKIE_DOMAIN=.whalewatch.com
  // shares it between both surfaces, so nothing rides in the hash.
  handoff: async () => '',
  receiveHandoff: async () => false,
}
```

### Behaviour the forms rely on

- **Password rule:** at least 10 characters (≤ 1000), the FE's `PASSWORD_MIN_LENGTH`. A new password equal to the current one is `422 weak_password`.
- **A password change** stamps `password_changed_at` (a `GET /v1/account` right after the `204` has it) and signs out every other session. The current one survives.
- **A password reset** signs out every session of the account. A weak `new_password` is refused before the token is spent, so the user can retry with the same link.
- **Login takes the same time for a wrong email and a wrong password**, and both answer `403 invalid_credentials`. A password reset request answers `204` as fast whether or not the address has an account: the email goes after the answer.
- **Signups:** `SIGNUPS_OPEN=false` by default, matching `FEATURES.signupsOpen`. Registration validates the fields, records the request and answers `recorded`. Accounts are made with the `create-user` CLI verb, and count as verified.
- **Terms and marketing consent are two checkboxes.** `accept_terms` must be `true` (required; link the terms and the privacy policy next to it). `marketing_email` is its own checkbox, **unticked by default**, never merged into the terms box; left out, it's `false`. The API records both, with the document versions, as the account's lawful-basis ledger (§5a).
- **Open signups start with the link, not a session.** `202 { status: 'check_email' }`, no cookie, whether or not the address has an account. A new address gets a verification link (24 hours, one use); an address with a verified account gets a "someone tried to register" note; one with an unverified account gets a fresh link, and keeps the name and password it was made with. Following the link verifies the address **and starts the session**: `POST /v1/auth/verify-email` answers `200 Session` and sets the cookie. An unverified account can still log in with its password: it browses, and its watchlist writes are `403 email_unverified`. A new password (reset or change) ends any verification link still out, since the link signs in. A password reset link verifies the address too.

### 5a · Privacy: choices, export, deletion

| Endpoint | Body | Answer |
|---|---|---|
| `GET /v1/me/privacy` | none, signed in | `Privacy`: `{ terms: { accepted_version, accepted_at, current_version, privacy_version }, purposes: [{ purpose, basis, granted, changeable, since }] }` |
| `PATCH /v1/me/privacy` | `{ product_email?, marketing_email? }` | `Privacy`. Left out or `null` stays as it is. Works unverified |
| `GET /v1/me/export` | none, signed in | `Export`, with `Content-Disposition: attachment; filename="whalewatch-export-YYYY-MM-DD.json"` |
| `DELETE /v1/account` | `{ password }` | `204`, cookie cleared. `403 invalid_credentials` for a wrong password |

- **Purposes** come in a fixed order: `account` (`contract`, always granted, `changeable: false`), `product_email` (`legitimate_interest`, on by default, the user may object), `marketing_email` (`consent`, off unless given). Render the two `changeable` ones as toggles on the account page.
- **Export:** fetch it with `credentials: 'include'` and save the blob; a plain `<a href>` to the API also works, since the cookie goes with it.
- **Deletion** signs out every session at once and frees the address. The data is erased 30 days later; support can restore it until then. After the `204`, clear the session query and go to the marketing site. Ask for the password in a confirm dialog, and show the 30-day note there.
- `terms.accepted_version` is `null` for accounts made before terms existed. Nothing asks them to accept new terms yet.

### 5b · API keys and monthly usage (TIER-5)

An account on a plan with API calls (`entitlements.api_calls_month` above 0: the API plans) makes keys for its programs on the account page. A program sends one as `Authorization: Bearer ww_…` to any data endpoint. It reads with the account's plan, and every request spends one of the month's `api_calls_month`. The website's own requests, signed in or not, never send a key and spend nothing.

| Endpoint | Body | Answer |
|---|---|---|
| `GET /v1/api-keys` | none, signed in | `{ data: ApiKey[] }`, oldest first: `{ id, name, prefix, created_at, last_used_at }`. Revoked keys are not listed |
| `POST /v1/api-keys` | `{ name }` (1–80 characters) | `201 CreatedApiKey`: an `ApiKey` plus `key`. **Show `key` once**, with a copy button: it is never sent again. `402 plan_limit` on a plan with no API calls (offer `detail.plan`), `403 email_unverified`, `422` past 10 keys |
| `DELETE /v1/api-keys/{id}` | none | `204`. The key stops working at once. Idempotent |

A key only reads data. It is not a session, so the account, the watchlist and the keys themselves answer `401 unauthenticated` to a request with only a key, and `/v1/me` answers as signed out. Deleting the account revokes every key.

**Usage on the account page.** `GET /v1/me` has `usage.api_calls_month` and `usage.mcp_calls_month`, each `{ used, limit, remaining, period_start, resets_at }`. Show "8,125 of 10,000 calls this month, resets 14 November" with a meter, so the user sees the wall coming. The month runs from the subscription's anniversary at midnight UTC, not from the 1st. Free's month runs from the account's anniversary. A count can be a few minutes behind when calls land on several servers.

**What a program sees.** Every keyed answer carries `X-RateLimit-Limit` (the month's quota), `X-RateLimit-Remaining` and `X-RateLimit-Reset` (Unix seconds). These replace the per-minute headers of the same names. A spent month is `429 quota_exceeded` with `Remaining: 0`, `Retry-After` (the seconds to the reset) and `detail { limit, allowed, used, resets_at, plan }`. A key whose plan has no calls (a lapsed subscription) is `402 plan_limit`. An unknown or revoked key is `401 invalid_api_key`.

At 80% of a month's quota the account gets one email: once per month, never twice.

---

## 6 · Pages the emails link to

Both links are built as `WEB_URL + path + ?token=<token>`. `WEB_URL` is the app surface (`https://app.whalewatch.com`; `http://app.localhost:5173` in dev). The paths are constants in `app/accounts/mail.py`. Say if you want different ones.

| Page | Path | What it does |
|---|---|---|
| Confirm email change | `/account/confirm-email?token=…` | `POST /v1/account/email/confirm { token }`. `204` → "Email changed", then invalidate the account and session queries. `422 invalid_token` → "This link has expired. Change your email again." `422 invalid_email` → the address was taken in the meantime. **Works signed out**: the link may be opened on another device |
| Verify email | `/verify-email?token=…` | `POST /v1/auth/verify-email { token }`. `200 Session` + cookie → "Email verified", signed in; invalidate `me`. `422 invalid_token` → offer a new link (`POST /v1/auth/verify-email/resend`, signed in; signed out, log in first). **Works signed out** |
| Reset password | `/reset-password?token=…` | A new-password form → `POST /v1/auth/password-reset/confirm { token, new_password }`. `204` → "Password changed. Log in." (every session was signed out). `422 invalid_token` → offer to send a new link. `422 weak_password` → under the field |

`AuthAdapter` needs one more method for the second, for example `confirmPasswordReset(token, next)`, and `DataSource` (or the account module) one for the first, for example `confirmEmailChange(token)`.

### 6a · Unsubscribe (ALR-3)

Every alert email (the season digest included) has an unsubscribe link in its footer: `WEB_URL + /unsubscribe?token=…`. Mailbox providers also post to the API directly from the message's `List-Unsubscribe` header, with no page in between, so this page is only the footer link's.

| Step | Call | What to show |
|---|---|---|
| On load | `GET /v1/email/unsubscribe?token=…` | `200 { mailing_list, kind, label, subscribed }`. Subscribed: "Unsubscribe from emails about {label}?" with one button. Not subscribed: "You're not getting emails about {label}" with nothing to press. **Never unsubscribe on load**: mail scanners open every link |
| The button | `POST /v1/email/unsubscribe?token=…` (no body) | `200`, the same shape with `subscribed: false` → "Done. You won't get emails about {label}." Then link to `/account` for the rest of the email settings |
| Either | `422 invalid_token` | "This unsubscribe link isn't valid any more. You can choose what WhaleWatch emails you from your account." with a link to `/account` |

**Works signed out, and needs no session**: the token is the proof. `label` is a phrase for after "about": "the 13F season digest", "new 13F filings by Berkshire Hathaway". For an alert, leaving takes email off that one alert and keeps it in the notification centre. Account emails (verification, password reset, billing) have no unsubscribe link at all.

---

## 7 · Open questions: what the API does today

| # | Question | What the API does now | To change it |
|---|---|---|---|
| Q1 | Password rule | **Settled:** ≥ 10 characters, the FE's rule | `PASSWORD_MIN_LENGTH` in `app/accounts/rules.py` and in the FE, together |
| Q2 | Default `time_zone` | **Settled:** `America/New_York` for every new account. The FE doesn't send `time_zone` on registration. The mock should seed New York too, not the browser's zone | |
| Q3 | Option A or B | **B** (BE-7 + BE-8) | |
| Q4 | Joint Form 4 filings | Unchanged: lines are under the first owner's CIK, so following a co-filer listed second matches nothing | Loader change, not an API one |
| Q5 | `insider_cik` on the feed | **Shipped**, with `filed_from` / `filed_to` and echoed in `meta` | |
| Q6 | Stock refs matching unlinked lines by issuer CIK | **Settled: yes.** An unlinked P/S line from the stock's issuer is under the followed stock, once (§4) | |

---

## 8 · Checklist for the FE PR

- [ ] `npm run gen:api` against this repo's `openapi.json`
- [ ] Aliases in `src/types/api.ts`: `AccountProfile`, `AccountPatch`, `AccountUpdate`, `Session` (as e.g. `ApiSession`), `SessionState`, `RegisterResult`, `WatchRef` (as `WireWatchRef`), `Watchlist`, `InsiderMatch`, `TimelineEvent` (as `WireTimelineEvent`), `UpcomingFiling` (as `WireUpcomingFiling`), `TimelineSide`
- [ ] Swap the hand-written `InsiderMatch` and `Watchlist` aliases in `src/api/source/types.ts` for the schema's
- [ ] `credentials: 'include'` on the client
- [ ] `apiSource`: account, watchlist, timeline and upcoming (§3, §4)
- [ ] `apiAuth`: every method (§5)
- [ ] Optional: `pendingEmail` on the FE `AccountProfile`, read from `GET /v1/account`
- [ ] Pages: verify email, confirm email change, reset password (§6)
- [ ] `GET /v1/me` for the app shell; gate saving on `entitlements.save`, with a resend-link banner for `email_unverified`
- [ ] Treat `503 unavailable` as "try again", never as signed out
- [ ] Registration: required `accept_terms` checkbox with terms + privacy links; separate unticked `marketing_email` checkbox; map `terms_not_accepted` (§5)
- [ ] Footer: links to the terms and the privacy policy
- [ ] Account page: email toggles, "Download my data", "Delete account" (§5a)
- [ ] First-sign-in watchlist migration (§4)
- [ ] Remove the `501` stubs for these methods. Saved filters and recents still have no endpoints
