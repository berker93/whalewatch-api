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
| `AuthAdapter.register` | `POST /v1/auth/register` | BE prereq | `recorded` while signups are closed |
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
6. **Registration takes an optional `time_zone`.** Send the browser's: see Q2 in §7.
7. **One new error code, `email_taken`** (409), for registering an address that already has an account. `AuthErrorCode` has no code for it yet (§5).

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

Auth, account and watchlist responses carry `Cache-Control: no-store`. The timeline isn't cached by the API either. Keep React Query's own caching as it is; just never persist these responses anywhere shared.

### Errors

Errors keep the existing shape, `{ error: { code, message, detail, request_id } }`, so `SourceError` / `toProblem()` already read them. What's new:

| Status | `code` | Where | Meaning |
|---|---|---|---|
| 401 | `unauthenticated` | any signed-in route | The request carried no session cookie |
| 401 | `session_expired` | any signed-in route, `GET /v1/auth/session` | The cookie named a session that ended, ran out or was signed out. The response also clears the cookie |
| 403 | `invalid_credentials` | login, change password | Wrong email or password, or wrong current password. **Never a 401** |
| 403 | `forbidden_origin` | any write | The `Origin` isn't on the API's allow-list. A config error, not a user error |
| 409 | `email_taken` | register | The address already has an account (only while signups are open) |
| 422 | `validation_error` | everywhere | `detail.errors[].loc` is `["body", "<wire field>"]` or `["query", "<param>"]` |
| 422 | `weak_password` / `invalid_email` / `invalid_name` | register, change password, reset | Same `detail.errors` shape, with its own code |
| 422 | `invalid_token` | email confirm, reset confirm | The link is unknown, used, replaced or expired |
| 429 | `rate_limited` | login, register, password calls, confirm links | 10 per minute per IP by default. `Retry-After` is set |

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
- **Rules are the FE's or looser:** name ≥ 2 characters once trimmed (≤ 200), email matches `EMAIL_PATTERN` (≤ 254), any IANA zone Postgres knows. `name` is stored trimmed, `email` lowercased.
- **A new email waits.** `profile.email` keeps the old one, `pending_email` names the new one, a link goes to the new address and a notice to the old. Sending the current email again is "no change" and cancels a waiting change. A second change replaces the first, and the first link stops working.
- **Confirming changes the login.** After `POST /v1/account/email/confirm`, `GET /v1/auth/session` reports the new address and the old one no longer logs in.

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
- `PUT /v1/watchlist/items` `{ ref }` follows; `DELETE /v1/watchlist/items` `{ ref }` unfollows. Both are idempotent and answer with the whole watchlist. A ref that names nothing is a `404` either way. A malformed CUSIP or CIK is a `422` with `loc: ["body","ref","cusip"]`. The cap is 500 items.

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
- A Form 4 line with no linked security (`trade.cusip === null`) never matches a followed stock (Q6).
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
| `POST /v1/auth/register` | `{ name, email, password, time_zone? }` | `{ status: 'recorded' }` or `{ status: 'authed', session }` + cookie |
| `POST /v1/auth/password` | `{ current_password, new_password }` | `204` |
| `POST /v1/auth/password-reset` | `{ email }` | `204`, always |
| `POST /v1/auth/password-reset/confirm` | `{ token, new_password }` | `204` |

`Session` is `{ user: { email }, expires_at }`. The FE's `Session` only needs `user`.

`remember: false` gives a cookie without `Max-Age`, which ends with the browser, and a server-side session of 12 hours. `remember: true` gives 30 days.

### Error mapping to `AuthErrorCode`

| API `code` | `AuthErrorCode` |
|---|---|
| `invalid_credentials`, `invalid_email`, `invalid_name`, `weak_password`, `session_expired` | same |
| `unauthenticated` (from `changePassword`) | `session_expired`. TA-12: a 401 reads as "Your session ended" |
| `email_taken` | No FE code yet. Add `'email_taken'` to `AuthErrorCode` with copy like "That address already has an account. Log in instead.", or map it to `invalid_email` for now |
| `rate_limited`, anything else, network failure | `unavailable` (TA-12: a `rate_limited` code can come later) |

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
  if (code === 'email_taken') return new AuthError('invalid_email') // until AuthErrorCode has it
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
    const time_zone = Intl.DateTimeFormat().resolvedOptions().timeZone
    const result = await call(
      api.POST('/v1/auth/register', { body: { name, email, password, time_zone } }),
    )
    return result.status === 'authed'
      ? { status: 'authed', session: toSession(result.session) }
      : { status: 'recorded' }
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
- **Login takes the same time for a wrong email and a wrong password**, and both answer `403 invalid_credentials`.
- **Signups:** `SIGNUPS_OPEN=false` by default, matching `FEATURES.signupsOpen`. Registration validates the fields, records the request and answers `recorded`. Accounts are made with the `create-user` CLI verb.

---

## 6 · Two pages the FE doesn't have yet

Both links are built as `WEB_URL + path + ?token=<token>`. `WEB_URL` is the app surface (`https://app.whalewatch.com`; `http://app.localhost:5173` in dev). The paths are constants in `app/accounts/mail.py`. Say if you want different ones.

| Page | Path | What it does |
|---|---|---|
| Confirm email change | `/account/confirm-email?token=…` | `POST /v1/account/email/confirm { token }`. `204` → "Email changed", then invalidate the account and session queries. `422 invalid_token` → "This link has expired. Change your email again." `422 invalid_email` → the address was taken in the meantime. **Works signed out**: the link may be opened on another device |
| Reset password | `/reset-password?token=…` | A new-password form → `POST /v1/auth/password-reset/confirm { token, new_password }`. `204` → "Password changed. Log in." (every session was signed out). `422 invalid_token` → offer to send a new link. `422 weak_password` → under the field |

`AuthAdapter` needs one more method for the second, for example `confirmPasswordReset(token, next)`, and `DataSource` (or the account module) one for the first, for example `confirmEmailChange(token)`.

---

## 7 · Open questions: what the API does today

| # | Question | What the API does now | To change it |
|---|---|---|---|
| Q1 | Password rule | ≥ 10 characters, the FE's rule | `PASSWORD_MIN_LENGTH` in `app/accounts/rules.py` and in the FE, together |
| Q2 | Default `time_zone` | `America/New_York`, unless registration sends `time_zone`. The adapter above sends the browser's, so in practice the browser's wins | Drop `time_zone` from the register call to get New York |
| Q3 | Option A or B | **B** (BE-7 + BE-8) | |
| Q4 | Joint Form 4 filings | Unchanged: lines are under the first owner's CIK, so following a co-filer listed second matches nothing | Loader change, not an API one |
| Q5 | `insider_cik` on the feed | **Shipped**, with `filed_from` / `filed_to` and echoed in `meta` | |
| Q6 | Stock refs matching unlinked lines by issuer CIK | Not done: a line with `cusip: null` never matches a followed stock | Say so if you want it. It's one condition in `app/api/routers/timeline.py` |

---

## 8 · Checklist for the FE PR

- [ ] `npm run gen:api` against this repo's `openapi.json`
- [ ] Aliases in `src/types/api.ts`: `AccountProfile`, `AccountPatch`, `AccountUpdate`, `Session` (as e.g. `ApiSession`), `SessionState`, `RegisterResult`, `WatchRef` (as `WireWatchRef`), `Watchlist`, `InsiderMatch`, `TimelineEvent` (as `WireTimelineEvent`), `UpcomingFiling` (as `WireUpcomingFiling`), `TimelineSide`
- [ ] Swap the hand-written `InsiderMatch` and `Watchlist` aliases in `src/api/source/types.ts` for the schema's
- [ ] `credentials: 'include'` on the client
- [ ] `apiSource`: account, watchlist, timeline and upcoming (§3, §4)
- [ ] `apiAuth`: every method (§5)
- [ ] `AuthErrorCode`: decide on `email_taken`
- [ ] Optional: `pendingEmail` on the FE `AccountProfile`, read from `GET /v1/account`
- [ ] Pages: confirm email, reset password (§6)
- [ ] First-sign-in watchlist migration (§4)
- [ ] Remove the `501` stubs for these methods. Saved filters and recents still have no endpoints
