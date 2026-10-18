# Backend requirements — Epic 9 (Timeline + Account)

What `whalewatch-api` needs to add or change so the Timeline and Account screens can run on `VITE_DATA_SOURCE=api` / `VITE_AUTH_SOURCE=api` instead of the mock.

**Frontend epic:** `docs/epic-timeline-account.md` (TA-1 → TA-14)
**Frontend contracts:** `src/api/source/types.ts` (`DataSource`, `WatchRef`, `TimelineEvent`, `UpcomingFiling`), `src/types/account.ts`, `src/modules/Auth/adapter.ts` (`AuthAdapter`)

---

## 0 · How this file works

- **It's a living file.** Each TA task adds what it learned here: a new requirement, a field it found missing, an answered question. Put the task id in the *Added by* column.
- **The frontend doesn't wait for it.** Every method below already exists on `DataSource` or `AuthAdapter`. The mock implements it, and `apiSource` / `apiAuth` reject with `501` / `unavailable` until the endpoint ships. When the endpoint ships, run `npm run gen:api`, swap the alias in `src/types/` for the schema's type, and implement the method in `apiSource`. Nothing outside `src/api/source/` and `src/modules/Auth/internal/` changes.
- **Wire names are snake_case**, like the rest of the API. Where a frontend field differs (`timeZone` ↔ `time_zone`), the mapping is noted.
- **Status:** `proposed` → `agreed` → `shipped`. Only the developer changes a status.

---

## 1 · Summary

| ID | Need | Proposed endpoint | FE seam | Unblocks | Added by | Status |
|---|---|---|---|---|---|---|
| BE-1 | Read and edit the account profile | `GET` / `PATCH /v1/account` | `getAccount`, `updateAccount` | TA-9, TA-11, TA-13 | TA-1, TA-9, TA-11, TA-13 | shipped |
| BE-2 | Confirm an email change | `POST /v1/account/email/confirm` | `updateAccount` → `pendingEmail` | TA-9, TA-11 | TA-1, TA-9, TA-11 | shipped |
| BE-3 | Change password | `POST /v1/auth/password` | `AuthAdapter.changePassword` | TA-12 | TA-1, TA-9, TA-12 | shipped |
| BE-4 | Watchlist that holds stocks, investors **and insiders** | `GET /v1/watchlist`, `PUT` / `DELETE /v1/watchlist/items` | `getWatchlist`, `toggleWatch` | TA-2, TA-8 | TA-1, TA-8 | shipped |
| BE-5 | Form 4 lines by insider, windowed by filing date | params on `GET /v1/insiders/transactions` | `getTimeline` | TA-4 | TA-1, TA-6 | shipped |
| BE-6 | Filing date on 13F change rows, traded value on owner rows | fields on `Activity`, `StockOwner` | `getTimeline` | TA-4 | TA-1, TA-3 | not needed (option B) |
| BE-7 | The timeline itself | `GET /v1/timeline` | `getTimeline` | TA-4 | TA-1, TA-4, TA-6, TA-7 | shipped |
| BE-8 | Filings still due this period | `GET /v1/timeline/upcoming` | `getUpcomingFilings` | TA-4 | TA-1, TA-4 | shipped |

**BE-5 + BE-6 or BE-7 + BE-8: one pair is needed, not both.** See §3. *Shipped: option B (BE-7 + BE-8), plus BE-5 (Q5). How to call each, and where the wire differs from the proposals below, is in `docs/frontend-integration.md` in whalewatch-api.*

**Prerequisite, outside this epic:** *(shipped: `/v1/auth/login`, `logout`, `session`, `register`, `password`, `password-reset`, `password-reset/confirm`)* the API has no auth endpoints yet (`apiAuth` rejects everything with `unavailable`). BE-1 to BE-4 assume a signed-in session: an httpOnly cookie on `.whalewatch.com`, as `src/modules/Auth/internal/apiAdapter.ts` expects. Without one they answer `401`.

---

## 2 · Account

### BE-1 · Account profile
`GET /v1/account` · `PATCH /v1/account`

**Why:** the account page shows and edits the profile, and the header avatar shows the initials (TA-11, TA-13). The user's time zone groups the timeline by day and formats Form 4 times across the app.

**Proposed schema**
```yaml
AccountProfile:
  name: string                    # FE: name. Same rules as registration: ≥ 2 chars once trimmed
  email: string                   # FE: email
  time_zone: string               # FE: timeZone. IANA, e.g. America/New_York
  member_since: date-time         # FE: memberSince
  password_changed_at: date-time  # FE: passwordChangedAt. Signup time if never changed

AccountPatch:                     # PATCH body. Omitted fields stay as they are
  name?: string
  email?: string                  # starts BE-2; does not change `email` yet
  time_zone?: string

AccountUpdate:                    # PATCH response
  profile: AccountProfile
  pending_email: string | null    # FE: pendingEmail. Where the confirmation link went
```

**Errors:** `401` no session · `422` `ValidationProblem`, with `detail` by field (`name` too short, `email` malformed or taken, `time_zone` not an IANA zone). *(TA-9)* Each entry's `loc` is `["body", "<wire field>"]`, e.g. `["body", "time_zone"]`, as the mock answers. The account page puts the message under the field `loc` names, so the field name must be the wire name.

**AC**
- [ ] `GET` returns the signed-in user's profile
- [ ] `PATCH` applies `name` and `time_zone` immediately and answers with the updated profile
- [ ] `PATCH` with `email` leaves `profile.email` unchanged and sets `pending_email` (BE-2)
- [ ] `PATCH` is all or nothing: one invalid field → `422`, and none of the others is applied *(TA-9)*
- [ ] `name` is stored trimmed and `email` lowercased, as login and registration treat them *(TA-9)*. `PATCH` with the current email is not a change: `pending_email` stays `null` and no link is sent.
- [ ] `time_zone` is validated against the IANA database. Default for new accounts: **open question Q2**. *(TA-9)* Until it's answered, the mock seeds the browser's zone, the zone the timeline already falls back to while the account loads.

**What TA-10 settled.** The account page edits one field at a time (`EditableField` in `src/modules/Account/`), so each Save sends a `PATCH` holding **only that field**. A value saved unchanged sends nothing. Nothing new is needed from the BE. The planned sections in the account nav (subscription, billing, team) have no routes and call nothing (§5).

**What TA-11 settled.** The profile section (`src/modules/Account/ProfileSection.tsx`) runs on `GET` / `PATCH` as proposed. What it relies on:
- [ ] **A refused value is a `422` on its own field.** The page reads the last part of each `loc` (`refusedFields` in `src/api/source/errors.ts`) and shows the field's own message under it: an `email` refusal reads "That email address can't be used. Try another.", a `time_zone` one "That time zone isn't available. Pick another." So an email already on another account is a `422` on `["body", "email"]`, not a `409`. Any other failure reads "Couldn't save. Try again." *(TA-11)*
- [ ] **`GET /v1/account` is on every app page now**, beside `GET /v1/watchlist` (BE-4). The app shows every timestamp in the account's zone, not only the timeline's: Form 4 clock times, the overview's "Filed" times, `MEMBER SINCE`. Until it answers, times show in the browser's zone. Keep it a single cheap read. *(TA-11)*
- **The zone list is the FE's.** The select offers 19 curated zones (`src/constants/timeZones.ts`). `time_zone` still takes any IANA zone, and an account whose zone isn't on the list (one seeded from the browser, Q2) is offered its own zone at the top, named by its city. No list endpoint is needed.
- **Name and email are checked as registration checks them** (`nameError`, `emailError` in `src/modules/Auth/validation.ts`) before the `PATCH` is sent. The BE's own rules (BE-1 errors) must be the same or looser, or a value the form accepts comes back as a `422`.

**What TA-13 settled.** The header's account button (`src/modules/AppLayout/AccountMenuButton.tsx`) is a link to `/account` showing the initials of `name`: the first letter of its first two words, uppercased (`initials` in `src/modules/Account/initials.ts`). It replaces the account dropdown, so the header calls nothing new. What it relies on:
- [ ] **`name` comes with every `GET /v1/account`.** The header reads the same request TA-11 already puts on every app page; there is no separate avatar or initials field, and no image. Until the `GET` answers, or if it fails (`401`, the `501` stub), the button shows `·`. *(TA-13)*
- [ ] **`PATCH` answers the full updated profile** (BE-1 AC). The header's initials change from that answer, before the account is read again. *(TA-13)*

### BE-2 · Email change confirmation
`POST /v1/account/email/confirm` `{ token }`

**Why:** TA-9 shows "Confirmation link sent to …" and changes the stored email only once the user confirms.

**AC**
- [ ] `PATCH /v1/account` with a new `email` sends a confirmation link to the **new** address, and a notice to the old one
- [ ] Confirming swaps `email` and clears `pending_email`. An expired or reused token → `422`
- [ ] A second change before confirmation replaces the pending one
- [ ] `GET /v1/account` returns `pending_email` while a change is waiting, so a reload still shows it. *(The FE contract only carries `pendingEmail` on the update response today. If the BE adds it to `AccountProfile`, the FE adds the field. TA-9 didn't need it: the mock confirms at once, so the next read already has the new email.)*
- [ ] Confirming changes the **login** email too. The session's user (`Session.user.email`, what `getSession` answers) reports the new address from then on, and the old one no longer logs in. *(TA-9)*
- *(TA-11)* While a change waits, the email field reads "Confirmation link sent to {pending_email}", taken from the `PATCH` answer. It goes once `GET` returns the new email. It doesn't survive a reload until `GET` carries `pending_email` (the AC above): with it, the FE would show the line from the profile instead.

### BE-3 · Change password
`POST /v1/auth/password` `{ current_password, new_password }` → `204`

**Why:** TA-12's change-password form. It lives with the other credential calls (`AuthAdapter`), not on `/v1/account`.

**Errors:** `401` no session · `403` `invalid_credentials` when `current_password` is wrong · `422` `weak_password` when `new_password` breaks the rules. The FE maps these to `AuthErrorCode`, so the codes matter more than the statuses.

**AC**
- [ ] The rules match registration exactly, enforced server-side. Today's FE rule is **≥ 10 characters** (`PASSWORD_MIN_LENGTH`); the epic says 12 plus a letter and a number. **Open question Q1.**
- [ ] `new_password` must differ from `current_password`. *(TA-9)* The mock doesn't check this; the TA-12 form does. The BE answers `422` `weak_password`, the only code `AuthErrorCode` has for a refused new password.
- [ ] Success updates `password_changed_at` and **signs out every other session**. The success toast promises that ("other sessions signed out"). The current session survives.
- [ ] Rate-limited like login (`429` + `Retry-After`)

**What TA-12 settled.** The change-password form (`src/modules/Account/ChangePasswordForm.tsx`, in the Security section) runs on BE-3 as proposed. What it relies on:
- **The rule is registration's, today ≥ 10 characters.** The epic's checklist asked for 12 plus a letter and a number; the repo's rule wins until Q1 is answered. Both forms now check one validator (`passwordError` in `src/modules/Auth/validation.ts`), so answering Q1 changes `PASSWORD_MIN_LENGTH` / `passwordError` once and both follow. The BE's rule must be the same or looser, or a password the form accepts comes back as `422`. The form's checklist: the length rule · different from the current password · confirmation matches. Only the first two reach the BE; the confirmation never leaves the browser. *(TA-12)*
- [ ] **A wrong current password is `403` `invalid_credentials`, never `401`.** The form shows it under the current-password input ("That isn't your current password.") and keeps the form open. A `401` means the session is gone and reads as "Your session ended. Log in again." *(TA-12)*
- [ ] **A refused new password is `422` `weak_password`.** It shows under the new-password input. *(TA-12)*
- [ ] **A `GET /v1/account` right after the `204` already has the new `password_changed_at`.** The FE reads the account again on success and shows "Last changed {date} · N days ago" from it, counted in the account's zone. *(TA-12)*
- A `429` has no `AuthErrorCode` of its own, so today it reads as the generic "Couldn't reach Whalewatch. Try again in a moment." If the BE wants the wait shown, the FE needs a `rate_limited` code and `Retry-After`. Not needed for v1. *(TA-12)*
- **Two-factor** is a planned row in the Security section: muted, a `PLANNED` pill, no control. It calls nothing (§5). *(TA-12)*

---

## 3 · Timeline

The timeline is everything that happened to what the user follows, newest first, over the last 14–120 days. It mixes Form 4 lines with 13F filings. Its rules are in the epic's TA-3, and the FE shape is `TimelineEvent` in `src/api/source/types.ts`.

**The backend decides where it's built** (epic TA-1, *Notes*). Either way the FE calls only `getTimeline` and `getUpcomingFilings`, so the choice stays inside `src/api/source/`.

| | Option A: client builds it (BE-5 + BE-6) | Option B: API serves it (BE-7 + BE-8) |
|---|---|---|
| BE work | Two additive changes to existing endpoints | Two new endpoints |
| Requests per load | About 2 per followed stock, 2 per investor, 1 per insider, plus paging. ~30 for 12 refs | 2 |
| Where TA-3's rules live | FE only (`buildTimeline`) | BE, with `buildTimeline` kept as the mock and the test oracle |
| Risk | Late filers and paging edges are handled in the browser | None beyond the endpoint |

**Recommendation: B**, if the BE can take it this cycle. Option A works with smaller changes, but every load fans out per followed item.

**What TA-4 settled** (either option)
- **The FE sends the time zone.** `TimelineQuery` and `UpcomingFilingsQuery` carry `timeZone`, the account's IANA zone (BE-1), and the FE always fills it in. On the wire it is `tz`. `from` / `to` are days in it. The FE groups the feed by day in the same zone, so a zone the BE picks for itself could move an event across a day line.
- **"Today" is the API's clock.** The FE takes it from `meta.generated_at` on `GET /v1/meta/periods`, read in `tz`, never from the browser. The window it asks for is `[today − 119, today]`, 120 days by default (`FEATURES.timelineWindowDays`, clamped to 14–120).
- **One request per window.** `getTimeline` answers with every event in the window. It takes no cursor. If BE-7 pages, `apiSource` reads every page before answering, as `fetchAllPages` does for the other "all" queries.
- **No Form 4 lines until Form 4 is loaded.** `/v1/meta/data-sources` reports `insider_transactions` as `absent` today, so the timeline has 13F events only. The mock does the same by default. `?mock=form4` loads seeded Form 4 lines for the insiders in `INSIDERS` and reports the source as `sec_derived`. Nothing else changes when the real lines arrive.

### BE-4 · Watchlist with insiders
`GET /v1/watchlist` · `PUT /v1/watchlist/items` `{ ref }` · `DELETE /v1/watchlist/items` `{ ref }`

**Why:** the watchlist had no endpoint before this epic (it lives in `localStorage` under `ww:watchlist`). This epic adds a third kind, and the timeline and the `/` redirect (TA-8) both depend on it, so it can't stay browser-only once the app runs on the API.

**Proposed schema**
```yaml
WatchRef:                          # one of
  { kind: stock,    cusip: string }        # by CUSIP, never by ticker: tickers change
  { kind: investor, slug: string }
  { kind: insider,  insider_cik: string }  # FE: insiderCik. Never by name

Watchlist:
  securities: StockMatch[]
  investors:  InvestorMatch[]
  insiders:   InsiderMatch[]       # new

InsiderMatch:                      # new. What the watchlist sheet's insider row shows
  insider_cik: string
  insider_name: string             # as the latest filing spells it
  roles: Role[]                    # on their latest filing
  officer_title: string | null
  issuer_name: string              # the issuer of their latest filing
  current_ticker: string | null
```

**AC**
- [ ] `PUT` adds and `DELETE` removes. Both are idempotent and answer with the whole `Watchlist`. (The FE's `toggleWatch` picks one by what it holds.)
- [ ] An unknown CUSIP, slug or CIK → `404`
- [ ] The FE migrates the old browser-only list into the account on first sign-in: one `PUT` per item. No bulk endpoint needed.
- [ ] `GET` and both writes always send all three lists, `insiders` included, `[]` when empty. The header count and the `/` redirect (TA-8) add up all three. *(TA-2)*
- [ ] Each list keeps the order items were added in, oldest first. The FE appends a new item before the answer arrives, so a reordered answer makes rows jump. *(TA-2)*

**What TA-2 settled**
- The FE contract is in place: `InsiderMatch` and `Watchlist.insiders` in `src/api/source/types.ts`. `InsiderMatch` has the six fields above, picked from `InsiderTransaction`. When the schema gets its own `InsiderMatch`, swap the alias.
- **Old browser lists.** The mock reads `ww:watchlist` one entry at a time. A ticker-only entry (`"AAPL"` or `{ kind: 'stock', ticker }`) is moved to its CUSIP. An entry that no security answers to is kept in the browser as it was, never dropped. The first-sign-in migration `PUT`s only entries with a CUSIP, and an unresolved ticker stays in the browser. **No BE change:** the API's `WatchRef` stays CUSIP-only.
- A Form 4 row's star sends `{ kind: insider, insider_cik }` and shows the row's own name, roles and issuer until the answer arrives. The `InsiderMatch` the BE returns (from the insider's **latest** filing) then replaces it. If the two differ, the sheet shows the BE's.

**What TA-8 settled.** `/` is not a page. It waits for `GET /v1/watchlist`, then replaces itself with `/timeline` if any of the three lists holds something, or `/overview` if none does. `/timeline` with an empty watchlist does the same, and so does unfollowing the last item there. The router code is `src/modules/Router/HomeRedirect.tsx` and `RequireFollowing.tsx`.
- [ ] **`GET /v1/watchlist` is on the path of every app entry.** Login, the auth callback's default `next`, the wordmark and the marketing site's "Open the app" all land on `/`, and no page shows until the watchlist answers (a skeleton holds meanwhile). Keep it a single cheap read: no paging, no per-item lookups the client waits on. *(TA-8)*
- [ ] **Nothing followed is `200` with three `[]`**, never `404` or `204`. An empty answer sends `/` to the overview. An error sends `/` to the overview as well, but on `/timeline` it shows "Couldn't load the timeline." with a Retry, so the two must stay distinguishable. *(TA-8)*
- [ ] **Links the BE sends into the app** (the BE-2 confirmation email, any future notification) **point at `/`** or at a specific page, never at `/overview` as "home". `/` picks the right page. *(TA-8)*
- The first-sign-in migration (AC above) must finish before the first `GET` that `/` acts on. Otherwise a user whose list was browser-only lands on the overview once. That ordering is the FE's job and needs no BE change. *(TA-8)*

### BE-5 · Form 4 lines by insider, by filing date *(option A)*
Additive query params on `GET /v1/insiders/transactions`:

| Param | Meaning |
|---|---|
| `insider_cik` | One or more CIKs, comma-separated or repeated |
| `filed_from`, `filed_to` | Window on `filed_at`, as ISO dates in UTC |

**Why:** the timeline follows insiders by CIK, and the feed can only filter by `ticker`. The timeline also places a Form 4 line by **when it was filed**, since that's when the market learned of it. The existing `from` / `to` filter on trade day, so a line filed late, inside the window, for a trade outside it, is missed.

**AC**
- [ ] `insider_cik` combines with `code=all`, so a followed insider's grants, exercises and gifts come back
- [ ] `filed_from` / `filed_to` combine with `ticker`, so one call per followed stock is enough
- [ ] `meta` echoes both, as it echoes `codes`
- [ ] `filed_at` is EDGAR's acceptance time, to the minute at least, in UTC, as it is today. The timeline prints a Form 4 by its clock time in the user's zone (`14:32`); a date stored as midnight would read `00:00`, or `20:00` the day before in New York, and land on the wrong day. *(TA-6)*

### BE-6 · Filing date on 13F change rows *(option A)*
Additive fields on `Activity` (`/v1/investors/{slug}/activity`) and `StockOwner` (`/v1/stocks/{ticker}/owners`):

```yaml
accession_no: string   # the filing the row is from
filed_at: date-time    # when it reached EDGAR
```

**Why:** the timeline dates every 13F event by filing date, never by period end (TA-3), and shows the reporting lag ("holdings as of Jun 30 · reported 18 days later"). Neither row says when its filing arrived. `FeedFiling` does, but `/v1/market/activity` can't filter by investor or date, and `InvestorSummary.last_filed_at` covers only the latest period.

`StockOwner` also needs the traded value *(TA-3)*:

```yaml
traded_value_usd: decimal   # as on Activity: unsigned, the shares moved at the period-end price; 0 for hold
```

A followed stock shows each quarter's **top 4 holders by |Δ value|**, where Δ value is `traded_value_usd`, the field `/activity` orders by. `/owners` is the only per-stock 13F endpoint, and its rows have `shares_delta` and `value_usd` but no traded value. Without it the FE would rank by a value it made up.

**AC**
- [ ] For a period restated by an amendment, `filed_at` is the filing the row's figures come from
- [ ] Present on every row, `hold` included
- [ ] `StockOwner.traded_value_usd` is computed exactly as `Activity.traded_value_usd` for the same holder, stock and period *(TA-3)*

### BE-7 · Timeline *(option B)*
`GET /v1/timeline?stock=…&investor=…&insider=…&from=…&to=…`

Each ref param can repeat. `from` / `to` are ISO dates, both included, counted in `tz`. The window is at most 120 days. Newest first; paged with `cursor` like the other feeds.

| Param | Meaning |
|---|---|
| `tz` | IANA zone the window's days are counted in. The FE always sends the account's *(TA-4)*. Omitted → the account's (BE-1) |

**Proposed schema** (mirrors the FE's `TimelineEvent`, snake_case)
```yaml
TimelineEvent:                     # discriminated by `kind`
  id: string                       # stable: accession + line (Form 4), accession + cusip (13F change), accession (13F summary)
  kind: insider_trade | holder_change | filing_summary
  source: form4 | 13f
  at: date-time                    # filed_at, always
  ref: WatchRef                    # what the user followed that put it here
  side: acquire | dispose | neutral
  value_usd: decimal | null        # signed. Null when the filing gives no price

  # kind = insider_trade
  trade: InsiderTransaction

  # kind = holder_change | filing_summary
  period: string                   # 2026Q2
  as_of: date                      # period end
  lag_days: integer                # as_of → filed_at
  accession_no: string
  investor: { slug, display_name }

  # kind = holder_change
  change: { cusip, ticker, current_ticker, issuer_name, action, shares_delta, shares_delta_pct, traded_value_usd }

  # kind = filing_summary
  counts: { new, add, trim, exit }
  largest_change: LargestChange | null
  position_count: integer
  portfolio_value_usd: decimal | null
```

**AC: the rules from TA-3, served**
- [ ] Followed stock: Form 4 **P and S only**. 13F: per filed period, the **top 4 holders by |Δ value|** whose action isn't `hold`
- [ ] Followed investor: one `filing_summary` per filing. `value_usd` = net Δ
- [ ] Followed insider: Form 4 **all codes**
- [ ] When an insider is followed **and** trades a followed stock: one event, `ref.kind = insider` (dedupe on accession + line)
- [ ] `side`: P, A, M → `acquire`; S, G → `dispose`; anything else → `neutral`
- [ ] Ties sort stably (`at` desc, then `id`)
- [ ] Decimals as strings, like the rest of the API
- [ ] `422` when `from` or `to` isn't a real date, `from` is after `to`, the window is over 120 days, or `tz` isn't an IANA zone. The mock answers the first three the same way *(TA-4)*
- [ ] A window under 14 days is answered as asked: `[from, to]`, nothing before *(TA-4)*
- [ ] For `insider_trade`, `at` is EDGAR's acceptance time to the minute, in UTC, never a date at midnight: the row prints it as a clock time in `tz` *(TA-6)*

**What TA-3 settled.** `buildTimeline()` in `src/modules/Timeline/buildTimeline.ts` implements these rules, and its tests are the spec. Under option B it stays as the mock and the oracle. The API's answer should match it on the same data. The details below go past the rules above:
- **Followed insider = every code**, not only P/S/A/M/G. `F`, `D`, `X` and the rest come through too, with `side: neutral`.
- ~~**A Form 4 line with no `cusip` never matches a followed stock.**~~ *Changed by Q6 (2026-10-19):* a P/S line with no `cusip`, filed by the followed stock's issuer, matches that stock, once, under the first followed class. `trade.cusip` stays null. It still shows for a followed insider too.
- **Signs.** Form 4: `value_usd` is negative when `acquired_disposed = D`, whatever the code, so an `F` is negative and `neutral`. Holder change: `traded_value_usd`, negative when `shares_delta < 0`. Filing summary: the sum of the filing's signed traded values, `hold` rows aside. Its `side` follows the sign of that sum. All values in dollars to the cent.
- **The top-4 cut is made over the whole quarter, before the window.** An event never appears just because a bigger one aged out of the window. Ties on |Δ value| go to the slug, A→Z.
- **An amended quarter counts once per holder**, from the latest filing that reports the stock. The event's `id` and `at` are that filing's.
- **No dedupe between 13F kinds.** A followed investor that is also a top-4 mover in a followed stock gives two events: its `filing_summary` and the `holder_change`. They answer different follows.
- **The window is counted in calendar days in the user's time zone**, on `at` (`filed_at`), today included: `[today − days + 1, today]`, with `days` clamped to 14–120.

**What TA-6 settled.** The rows (`TimelineEvent`, `TimelineUpcoming` in `src/modules/Timeline/`) render from the fields above. Nothing new is needed beyond the `filed_at` precision in BE-5 / BE-7. What they rely on:
- **A Form 4 row shows `at` as a clock time** (`14:32`) in the user's zone, and the trade date beside it when the two fall on different days (`traded Jul 28`). It reads `transaction_date` from `trade`.
- **`value_usd: null` is a real state**, not an error. The row prints `—` and draws no bar. Most grants and gifts have no price.
- **`largest_change.traded_value_usd` stays unsigned**, as on `FeedFiling`. The FE signs it by `action` (`trim` / `exit` negative).
- **An exit's shares come from `change.shares_delta`**: "sold its entire position of 905.56M shares" is `−shares_delta`. For an exit it must be minus the whole previous position.
- **Links use `current_ticker`**, then `ticker`, then the CUSIP (`/stocks/cusip:…`). A 13F row's link carries its `period`.
- **`UpcomingFiling` needs no names.** The FE takes the stock's ticker and the investor's name from the watchlist (BE-4), which it already holds.

**What TA-7 settled.** The page (`src/views/TimelineView/`) needs nothing new. What it relies on:
- **Nothing in the window is `200` with `[]`**, never a `404`. The page then prints `No filings in the last 120 days.`, or `Nothing from NVDA in the last 120 days.` under a filter. An error status shows "Couldn't load the timeline." and a Retry instead.
- **Filtering is the FE's.** The kind and entity filters narrow the one answer in the browser, so the chip counts stay the whole feed's. No filter params on BE-7.
- **The Timeline nav item shows only while the watchlist holds something.** Every app page already reads `GET /v1/watchlist` for the header count (BE-4), so this adds no request.

**What TA-14 settled.** The states, 375px and accessibility pass needs nothing new from the BE. What it relies on:
- **Each row reads to a screen reader as one sentence, built from fields already listed**: the entity, the headline, `value_usd`, and the source. For a Form 4 that is `at` as a clock time, so the `filed_at` precision in BE-5 / BE-7 matters to the ear as well as the eye. For a 13F it is `period`, with `as_of` and `lag_days` read as the row's description. An upcoming row reads its `due_at` the same way. No accessible-name or summary field is needed: the FE writes the sentence.
- **A null `value_usd` is left out of the sentence**, not read as a dash. Same rule as on screen (TA-6).
- The account's states on `/dev/states` run on stand-ins that never call `PATCH /v1/account` or `POST /v1/auth/password`, and its loading and error frames use the mock's `?mock=slow` / `?mock=error`. Nothing there is a BE concern.

### BE-8 · Upcoming filings *(option B)*
`GET /v1/timeline/upcoming?stock=…&investor=…&tz=…` → `UpcomingFiling[]`

`tz` as in BE-7: the zone that says which quarter is current on the day one ends *(TA-4)*.

```yaml
UpcomingFiling:
  ref: WatchRef                     # stock or investor only
  period: string                    # the current quarter, 2026Q3
  due_at: date                      # PeriodCoverage.filing_deadline
  pending_holders:                  # stock refs only
    count: integer                  # last period's holders that haven't filed this one
    first: { slug, display_name }[] # up to 3, largest holders first
```

**AC**
- [ ] Empty once the current period is fully covered (`filers_reported = filers_tracked`)
- [ ] A followed investor appears while it hasn't filed the current period
- [ ] A followed stock appears while ≥ 1 of its prior-period holders hasn't filed
- [ ] **The current period is the quarter that ended most recently**, in the user's time zone, not the latest one in `/v1/meta/periods`. From the day after a quarter end, every followed investor is upcoming, even before anyone has filed. A quarter with no filers yet is 0 % covered, and `due_at` is its end + 45 days. *(TA-3)*
- [ ] Prior-period holders are owners with shares at the prior period end. `/owners` lists exits with zero shares, and an exit has nothing left to report, so it is never pending. `first` is ordered by `value_usd`, largest first. *(TA-3)*
- [ ] Items in watchlist order: the order the refs were sent in *(TA-3)*

*Under option A the FE derives this from `/v1/meta/periods`, `InvestorSummary.latest_period` and the prior period's `/owners`. That works today with no BE change, at one extra call per followed stock.*

---

## 4 · Open questions

| # | Question | Raised by | Answer |
|---|---|---|---|
| Q1 | Password rule: ≥ 10 chars (the FE today) or ≥ 12 with a letter and a number (the epic)? Registration and change-password must share one rule, on both FE and BE. | TA-1 | **Settled (2026-10-19):** ≥ 10 characters, the FE's rule, on both FE and BE (`app/accounts/rules.py`). |
| Q2 | Default `time_zone` for new accounts: `America/New_York` (the market's clock) or the browser's at signup? | TA-1 | **Settled (2026-10-19):** `America/New_York` for every new account. The FE doesn't send the browser's zone, and the mock should seed New York too. |
| Q3 | Option A or option B for the timeline (§3)? | TA-1 | B, shipped. |
| Q4 | Joint Form 4 filings put each line under the **first** owner's CIK (`InsiderTransaction.roles` doc). Following a co-filer who isn't listed first matches nothing. Acceptable for v1? | TA-1 | Unchanged for v1: lines stay under the first owner's CIK. |
| Q5 | Should `insider_cik` on `/v1/insiders/transactions` (BE-5) ship even under option B? Nothing filters Form 4 by insider today, so the watchlist sheet links a followed insider to `/insiders?ticker=` + their latest issuer. With the filter, it could link to that insider's own lines. | TA-2 | Yes, shipped with `filed_from` / `filed_to`. |
| Q6 | A Form 4 line whose `cusip` is null (the issuer has no listed security, or two classes and an ambiguous symbol) never matches a followed stock today. Should a stock ref also match lines by the stock's **issuer CIK**? Under option B only the BE can do it. Under option A it needs the issuer CIK on `StockDetail`. | TA-3 | **Settled (2026-10-19): yes.** A stock ref also matches its issuer's unlinked P/S lines (issuer CIK from SEC's ticker list, as `/v1/stocks/{ticker}/insiders` does). One event, under the first followed class. `buildTimeline()` needs the same rule. |

---

## 5 · Out of scope

Same as the epic's §5: two-factor, subscription, payment, invoices, team/admin, notification preferences, account deletion. No endpoints for them.

Saved filters and recents have no endpoints either (`apiSource` rejects them with `501`), but they predate this epic and aren't tracked here.
