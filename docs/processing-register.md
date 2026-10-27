# Processing register

GDPR Art. 30: what WhaleWatch stores about people, why, on what basis, for how
long, and who else handles it. Written from the code as of migration 0039
(2026-10-25). Change this file in the same commit as the code it describes.

Items marked **OPEN** are not decided or not done. They are also listed in the
[last section](#open-items).

## Controller

| | |
|---|---|
| Controller | **OPEN**: legal entity name, address, contact email |
| Representative in the EU/UK (Art. 27) | **OPEN**: needed only if the entity is outside the EU/UK |
| DPO | Not required at this scale (no large-scale monitoring, no special categories). Contact for privacy: **OPEN** |

## 1 · Account holders

The people who sign up. Every table below is in Postgres unless it says Redis.

### What is stored

| Data | Where | Why | Lawful basis | Kept for |
|---|---|---|---|---|
| Email, name, time zone | `app_user` | Login, display, showing times in the user's zone | Contract (Art. 6(1)(b)) | Life of the account + 30 days |
| Password hash (argon2id) | `app_user.password_hash` | Login | Contract | Same |
| Created, password changed, email verified, last seen (to 5 min) | `app_user` | Security, account page | Contract | Same |
| Watchlist (stocks, investors, insiders followed) | `watchlist_item` | The product | Contract | Same |
| Saved screener filters (name, screener parameters) | `saved_filter` | The product | Contract | Same |
| Plan and subscription (plan, status, period end, cancel date, grandfathered plan, per-account entitlements) | `subscription`, `subscription_entitlement`; a copy of the grant in the Redis session | What the account may do | Contract | Same |
| Emailed-link tokens, as SHA-256 only; a pending new email | `account_token` | Verify, reset, change email | Contract | 1 h (reset) or 24 h (verify, change), or until used; deleted on account deletion |
| Session: user id, remember flag, expiry. Cookie stored as SHA-256 only | Redis `ww:session:*`, `ww:user-sessions:*` | Staying signed in | Contract | 12 h, or 30 days with "keep me signed in"; ended at once on sign-out, password change or reset, and deletion |
| Lawful-basis ledger: purpose, basis, granted, terms/privacy version, source, time | `consent_record` | Proof of consent, objection and terms acceptance (Art. 7(1)) | Legal obligation (Art. 6(1)(c)) | Life of the account + 30 days |
| Deletion audit: former user id, who asked, when asked, when purged or restored, row counts erased. No email, no name | `account_deletion` | Proof the erasure happened (Art. 5(2)) | Legal obligation | **Indefinitely**. It holds a bare id that matches nothing once purged. **OPEN**: confirm, or set e.g. 6 years |
| API keys: name, the first 11 characters, SHA-256 of the key, made, last used (to 5 min), revoked | `api_key` | Programmatic access the paid API plans sell | Contract | Life of the account + 30 days. Revoked on deletion |
| Monthly call counts per quota and quota month, and when the 80% email went | `usage_counter`; the live count in Redis `ww:usage:*` | Enforcing the plan's monthly quota, showing usage on the account page | Contract | Postgres: life of the account + 30 days. Redis: the quota month + 7 days |
| Sign-in attempt counters: SHA-256 of the email, client IP | Redis `ww:rl:attempts:*` | Stopping password guessing | Legitimate interest (Art. 6(1)(f)): security | 15 minutes |
| Request log lines: time, method, path, status, client IP, request id | API stdout (structlog JSON) | Operations, abuse, debugging | Legitimate interest: security and operations | **OPEN**: depends on where logs are shipped. Suggest 30 days |
| Mail failure log lines: recipient, subject | API stdout | Knowing a reset or verify mail did not arrive | Legitimate interest | Same as request logs |

Development only: `EMAIL_BACKEND=log` writes each email, link included, to the
log. Staging and prod refuse to start with it.

### Purposes and their basis

Recorded per user in `consent_record`. The code is the source of truth:
`app/db/models/privacy.py` (`BASES`) and `app/accounts/privacy.py`.

| Purpose | Basis | Default | User control |
|---|---|---|---|
| `account`: run the account, plus the emails it can't work without (verify, reset, email change, deletion notice, the once-a-month warning at 80% of a paid quota) | Contract | On; it is the account | Ends only by deleting the account |
| `product_email`: news about the product the user uses | Legitimate interest | On from registration | Object any time: `PATCH /v1/me/privacy {"product_email": false}`. **OPEN**: an unsubscribe link in each email |
| `marketing_email`: promotion, offers, paid plans | Consent (Art. 6(1)(a), PECR reg. 22) | **Off**. A separate, unticked checkbox at registration, never bundled with the terms | Give or withdraw any time: `PATCH /v1/me/privacy`. **OPEN**: unsubscribe link |

Neither email purpose is sent today: no code sends product or marketing email.
A sender must check `app.accounts.privacy.may_email` first.

The terms and privacy policy accepted are recorded by version
(`TERMS_VERSION`, `PRIVACY_VERSION` in `app/accounts/privacy.py`, currently
`2026-10-24`). These must match the dates on the documents whalewatch-web
publishes. `GET /v1/me/privacy` shows the accepted version next to the current
one. **OPEN**: an endpoint and UI to accept new terms when the version changes.

### Rights, and how each is met

| Right | How |
|---|---|
| Access, portability (Art. 15, 20) | `GET /v1/me/export`: one JSON file with the account, watchlist, saved filters, the whole consent ledger, live links and sessions, every subscription, every API key (never the key) and each month's call counts, and a list of what's left out and why (hashes, logs). Logs on request by email. |
| Rectification (Art. 16) | `PATCH /v1/account`: name, time zone, email (confirmed by link) |
| Erasure (Art. 17) | `DELETE /v1/account` with the password, or `delete-user --email` by an operator for a request by email. The account is soft-deleted at once: signed out everywhere, links dead, login refused, address free for a new account. The `purge-accounts` job (beat, daily 05:00 New York) hard-deletes it 30 days later with its watchlist, saved filters, consent records, tokens, subscriptions, API keys and usage counts, and writes the counts to `account_deletion`. `restore-user --email` undoes it within the 30 days. |
| Withdraw consent, object (Art. 7(3), 21) | `PATCH /v1/me/privacy` |
| Restriction (Art. 18) | **OPEN**: no mechanism. Handle by hand (operator) if asked |

Backups: a purged account survives in database backups until they rotate.
**OPEN**: backup retention period (suggest 30 days, and say so in the privacy
policy).

### Before an account exists: signup requests

While `SIGNUPS_OPEN=false`, registering stores email, name, the terms and
privacy versions accepted, and the marketing choice in `signup_request`.
Basis: steps prior to a contract at the person's request (Art. 6(1)(b)). The
row is deleted when the account is created (`register` with signups open, or
`create-user`), and its answers move into `consent_record`.
**OPEN**: requests never turned into accounts are kept with no limit. Suggest
deleting them after 12 months, or when signups open.

## 2 · People in SEC filings

Form 4 reporting owners (`insider`, `reporting_owner`) are mostly natural
persons: name, CIK, roles (director, officer, 10% owner), officer title, and
their trades. 13F filer records (`filer`) can name a person as manager. All of
it comes from public SEC EDGAR filings, which the law requires them to make.

| | |
|---|---|
| Purpose | Showing public insider and institutional trading data |
| Basis | Legitimate interest (Art. 6(1)(f)). The data is published by law for exactly this use |
| Kept for | Indefinitely: it is the historical record |
| Notice (Art. 14) | The privacy policy must say this data is processed, and where it comes from. Art. 14(5)(b) (disproportionate effort) covers not contacting each person. **OPEN**: put it in the policy |
| Objection | **OPEN**: no process. An objection is weighed case by case; the record stays public at the SEC either way |

## 3 · Processors and other recipients

| Recipient | Role | Personal data sent | In the code today | DPA on file |
|---|---|---|---|---|
| Email provider (SMTP) | Processor | Recipient address, message body | Yes: `SMTP_HOST`; no provider chosen | **OPEN** |
| Hosting (AWS, planned) | Processor | Everything above | No: not deployed | **OPEN** |
| Sentry (planned) | Processor | Whatever an error report carries: request path, IP, user id if attached | No: not integrated | **OPEN**. Turn off `send_default_pii` and scrub emails when adding it |
| Paddle (planned) | Paddle is merchant of record, so mostly a controller for payment data; a processor for what WhaleWatch sends it | Email, plan; card data never touches WhaleWatch | No: not integrated | **OPEN**. Paddle's DPA/terms |
| OpenFIGI | Not a recipient of personal data | CUSIPs only | Yes | Not needed |
| SEC EDGAR | Source, not a recipient | None sent (a contact email in the User-Agent, the operator's own) | Yes | Not needed |
| Yahoo Finance (`yfinance`) | Not a recipient of personal data | Tickers only | Optional quote provider | Not needed |

International transfers: AWS and Sentry regions, and the email provider's,
decide this. **OPEN**: pick EU regions, or rely on the provider's SCCs or the
EU-US Data Privacy Framework, and list it here.

## 4 · Cookies and browser storage

| Name | Set by | Kind | Purpose | Lifetime |
|---|---|---|---|---|
| `ww_session` | API | Cookie, httpOnly, SameSite=Lax, Secure where deployed | Keeps the user signed in | Browser session, or 30 days with "keep me signed in" |
| `ww:density` | Web | localStorage | Remembers the table density the user picked | Until cleared |
| `ww:watchlist` | Web | localStorage | Signed-out watchlist, kept on the device only | Until cleared |
| `ww:mock-session`, `ww:account` | Web | localStorage | Mock auth, development builds only | Until cleared |

All of these are strictly necessary, or are a feature the user asked for, so
they need no consent (ePrivacy Art. 5(3) exemption, PECR reg. 6(4)). **No
cookie banner is needed.** That changes the moment analytics, ads, a chat
widget or any third-party embed that sets or reads storage is added. Add it
here and add a banner in the same change.

## 5 · Security measures (Art. 32)

- Passwords: argon2id. Session cookies and emailed tokens: only SHA-256 stored.
- Sessions revocable at once (Redis). All sessions end on password reset and on deletion.
- Login, register, reset and verify-resend limited to 5 per 15 minutes per IP and per email.
- `Cache-Control: no-store` on every per-user response. Writes refused from origins not on the CORS list.
- **OPEN**: encryption at rest and in transit for the deployed database and Redis, backups, and access control on who can run CLI verbs in prod.

## Open items

From the privacy checklist (2026-10-24), what the API cannot do itself:

1. **Privacy policy and terms**: write them, publish them on whalewatch-web, link them in the footer and next to the registration checkbox, and set `TERMS_VERSION` / `PRIVACY_VERSION` to their dates.
2. **Registration form** (web): an `accept_terms` checkbox, required; a separate `marketing_email` checkbox, unticked by default. Map `terms_not_accepted` to the terms checkbox.
3. **Account page** (web): email choices (`/v1/me/privacy`), "Download my data" (`/v1/me/export`), "Delete account" with a password prompt (`DELETE /v1/account`).
4. **DPAs** with the email provider, AWS, Sentry and Paddle, once each is chosen.
5. Controller identity, privacy contact, log and backup retention, the retention of the deletion audit and of unconverted signup requests. A support address for the deletion notice to name: it says "contact support" while `EMAIL_FROM` is a no-reply address.
6. Unsubscribe links (no session) in product and marketing email, before the first one is sent.
7. Re-accepting changed terms (endpoint and UI).
8. The SEC-filings section of the privacy policy (Art. 14), and a process for objections.
