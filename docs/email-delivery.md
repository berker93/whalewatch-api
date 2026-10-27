# Email delivery

How WhaleWatch sends email, and what has to be in place before it sends the
first one. The code is `app/accounts/delivery.py` (the courier),
`app/accounts/postmark.py` (the provider), `app/accounts/mail.py` (the
messages) and `app/api/routers/email.py` (unsubscribe and webhooks).

## The shape

- **One provider, behind one seam.** Everything that emails calls
  `Courier.send(message)`. The courier hands the message to a `Mailer`:
  `PostmarkMailer` in staging and production, `LogMailer` in development, an
  outbox in tests. Swapping the provider means writing another `Mailer`, plus a
  webhook parser for the new provider's events.
- **One sending subdomain: `mail.whalewatch.com`.** All mail is From
  `no-reply@mail.whalewatch.com`. A deliverability problem (a blocklisting, a
  DMARC failure, a reputation hit) then stays on the subdomain and never
  reaches `whalewatch.com`, where staff mail and the site live. Staging and
  production refuse to boot with an `EMAIL_FROM` on a bare apex domain.
- **Two streams.** Each kind of email goes out on one of two Postmark message
  streams. Postmark keeps a separate sending reputation and a separate
  suppression list for each:

  | Stream | Postmark id | What | Unsubscribe |
  | --- | --- | --- | --- |
  | `transactional` | `outbound` | verify, registration attempt, email change, password reset, account deleted, quota warning, dunning | none: sent whatever the user's settings say |
  | `broadcast` | `broadcast` | alerts (one per notification of a rule with the email channel), the season digest included; product and marketing email when something sends them | footer link and one-click header, per list |

- **Both bodies, every send.** Each message is written once, as paragraphs and
  buttons (`app/accounts/email_layout.py`), and drawn twice: hand-written
  table HTML with inline styles only, and a plain-text body that has every
  link. No images, no web fonts, and no open or click tracking.

## Before the first send

Do these in order. Nothing is sent from a domain until step 4 passes.

### 1. DNS records on `mail.whalewatch.com`

Postmark's values for the domain are under *Sender signatures → mail.whalewatch.com
→ DNS settings*. The DKIM selector looks like `20261027120000pm`; it is
different for every domain.

| Purpose | Type | Name | Value |
| --- | --- | --- | --- |
| DKIM | TXT | `<selector>._domainkey.mail.whalewatch.com` | `k=rsa;p=<the key Postmark shows>` |
| Return path (SPF, aligned) | CNAME | `pm-bounces.mail.whalewatch.com` | `pm.mtasv.net` |
| SPF | TXT | `mail.whalewatch.com` | `v=spf1 include:spf.mtasv.net -all` |
| DMARC | TXT | `_dmarc.mail.whalewatch.com` | `v=DMARC1; p=quarantine; adkim=r; aspf=r; rua=mailto:dmarc@whalewatch.com` |

Notes:

- SPF is checked against the envelope sender. That is the return path
  `pm-bounces.mail.whalewatch.com`, which the CNAME hands to Postmark's own SPF.
  The TXT record on `mail.whalewatch.com` itself says nothing but Postmark
  sends as the subdomain.
- DMARC passes when DKIM passes, because DKIM signs as `mail.whalewatch.com`
  (aligned). SPF passes aligned too, through the return path.
- Start at `p=quarantine`: nothing but Postmark sends from this subdomain, so
  there is no legitimate sender that a strict policy would hurt. Move to
  `p=reject` once two weeks of aggregate reports (`rua`) show only Postmark.
  `p=none` passes the check below with a warning, because it enforces nothing.
- The apex is not touched. If nothing sends mail as `whalewatch.com` itself,
  `v=spf1 -all` and `_dmarc.whalewatch.com` `v=DMARC1; p=reject` on the apex
  stop anyone spoofing it. If staff mail runs on the apex, its SPF belongs to
  that mail provider.

### 2. Postmark

1. Create a server per environment (staging, production). Its default
   transactional stream is `outbound`.
2. Add a **Broadcast** stream with the id `broadcast`. In its settings, set
   *Unsubscribe handling* to **None**: this API handles unsubscribing per list
   (one alert, the digest), which Postmark's single per-address switch cannot
   express. With Postmark's handling on, a digest unsubscribe would stop every
   alert.
3. Add the sender signature for the domain `mail.whalewatch.com` and wait for
   DKIM and Return-Path to show *Verified*. Postmark will not send from an
   unverified domain.
4. On **both** streams, add a webhook:
   - URL: `https://postmark:<POSTMARK_WEBHOOK_SECRET>@api.whalewatch.com/webhooks/postmark`
     (HTTP basic auth: Postmark signs nothing, so this secret is the only
     proof a webhook is Postmark's).
   - Events: **Delivery, Bounce, Spam Complaint, Subscription Change**. Leave
     Open and Click off; tracking is off anyway.
5. Set *Message retention* on the server as low as Postmark allows (see the
   processing register).
6. Copy the server's API token.

### 3. Environment

```
EMAIL_BACKEND=postmark
EMAIL_FROM=WhaleWatch <no-reply@mail.whalewatch.com>
API_URL=https://api.whalewatch.com
WEB_URL=https://app.whalewatch.com
POSTMARK_SERVER_TOKEN=<server API token>
POSTMARK_WEBHOOK_SECRET=<long random string, the same as in the webhook URL>
EMAIL_UNSUBSCRIBE_SECRET=<long random string>
# POSTMARK_TRANSACTIONAL_STREAM=outbound
# POSTMARK_BROADCAST_STREAM=broadcast
```

Rotating `EMAIL_UNSUBSCRIBE_SECRET` breaks every unsubscribe link already
sent. Those links then land on a page saying the link is not valid, and the
account page still works. Don't rotate it without a reason.

### 4. Check the domain

```
uv run python -m app.cli check-email-domain --dkim-selector 20261027120000pm
```

It looks up SPF, the return path, the DKIM key and DMARC (falling back to the
apex's `sp=`). It exits 1, naming each missing record, until all four are
published. Run it in the deploy before the first send, and again after any
DNS change.

### 5. One real send

Ask for a password reset to a team mailbox on Gmail. In *Show original*,
check `SPF: PASS`, `DKIM: PASS` and `DMARC: PASS` with `mail.whalewatch.com`.
Then check that the message's `email_log` row went `sent`, then `delivered`
(the Delivery webhook arrived).

## What happens to each message

1. **Checks, at the moment of sending.** The courier refuses to send to an
   address on the suppression list for the message's stream. For a broadcast
   message, it also refuses if the user is no longer on its list (rule off,
   email channel removed, consent withdrawn). The refusal is logged
   `suppressed` with the reason. Because the check runs at send time, an
   unsubscribe counts from the moment it commits, even for a message queued
   before it.
2. **Log.** An `email_log` row is written as `sending`, before Postmark is
   called. Its id goes to Postmark as metadata, and every webhook hands it back.
3. **Send.** Postmark answers with its `MessageID` (row → `sent`), or refuses
   (`failed`). A 406 *inactive recipient* means Postmark has already stopped
   the address on that stream: the row goes `suppressed` and the address is
   added here too. Postmark has no idempotency key, so a send is retried (three
   tries) only when it cannot have arrived: no connection, a 429, a 5xx. A
   timeout waiting for the answer is logged `failed`. If the message did
   arrive, its Delivery webhook moves the row on anyway.
4. **Webhooks.** Each event moves the row up, never down: `sent` →
   `soft_bounced` → `delivered` → `bounced` → `complained`. Applying an event
   twice changes nothing.

Sending stays best effort. A failure is logged, and the request that asked
for the email still succeeds.

## Bounces and complaints

| Event | Row | Address suppressed for |
| --- | --- | --- |
| Hard bounce (`HardBounce`, `BadEmailAddress`, or anything Postmark marks `Inactive`) | `bounced` | **everything**: the mailbox does not exist |
| Soft bounce (`Transient`, `SoftBounce`, …) | `soft_bounced` | nothing: Postmark retries |
| Spam complaint about a **broadcast** email | `complained` | **broadcast only**: alerts and digests stop, password resets still go |
| Spam complaint about a **transactional** email | `complained` | everything |
| Subscription Change, suppressed | — | the stream's scope (a hard bounce: everything) |
| Subscription Change, reactivated (someone reactivated it in Postmark) | — | lifted for that stream |

A suppression is only ever widened. The list is keyed by a SHA-256 of the
trimmed, lower-cased address, with no account id. It outlives the account on
purpose: erasing an account must not restart mail to someone who reported it
as spam. A user who changes their address to a new one is not suppressed.

Support:

```
uv run python -m app.cli email-suppression --email ada@example.com          # why, since when
uv run python -m app.cli email-suppression --email ada@example.com --lift   # send again
```

Lifting it here does not reactivate it in Postmark. Reactivate it on each
stream in Postmark too. That sends a Subscription Change, which lifts it here
anyway.

## Unsubscribing

Every broadcast email is on exactly one list:

| List | What it stops | Leaving does |
| --- | --- | --- |
| `alert_rule:<id>` | one alert's email (the season digest is one) | removes `email` from the rule's channels and keeps it in the bell; a rule that only emailed is turned off (`is_active`) |
| `product_email` | product news | a `consent_record` row withdrawing it, source `unsubscribe` |
| `marketing_email` | offers | the same, for consent |

Each broadcast email carries two ways off its list:

- **The one-click header** (RFC 8058, as Gmail and Yahoo require of bulk
  senders): `List-Unsubscribe: <https://api.whalewatch.com/v1/email/unsubscribe?token=…>`
  and `List-Unsubscribe-Post: List-Unsubscribe=One-Click`. The mailbox provider
  posts to the API from its own servers, with no cookie and no Origin header.
- **The footer link**, to `https://app.whalewatch.com/unsubscribe?token=…`.
  The web page reads `GET /v1/email/unsubscribe?token=…` ("Unsubscribe from
  the 13F season digest?") and posts the same URL when the button is pressed.
  The GET changes nothing, so a mail scanner that opens every link
  unsubscribes nobody.

The token is `<user id>.<list>.<HMAC-SHA256>` under `EMAIL_UNSUBSCRIBE_SECRET`.
It never expires and is not tied to the address. It can unsubscribe that one
account from that one list, and do nothing else.

Account mail is on no list and has no unsubscribe link. Leaving any list
cannot stop a password reset, because nothing connects the two.
