"""Accounts: who is signed in, and the rules their names, emails and passwords follow.

``passwords``
    argon2id hashing, in a thread.
``sessions``
    Sessions in Redis and the emailed links' tokens in Postgres, both hashed.
``entitlements``
    What each plan lets an account do, from rows, and the one place it is
    asked: ``limit(viewer, key)`` and ``can(viewer, key)``.
``api_keys``
    The keys an account's programs read the data with, hashed.
``usage``
    The monthly quotas: counted in Redis per subscription anniversary, flushed
    to Postgres, and the 80% email claimed once.
``rules``
    What a name, an email, a password and a time zone must be: the frontend's
    rules or looser.
``mail``, ``email_layout``
    What the site emails, each as an HTML and a plain-text body.
``delivery``, ``mailer``, ``postmark``
    Sending it: the suppression and unsubscribe checks at send time, the
    ``email_log``, Postmark where deployed (the log in development), and
    Postmark's webhooks.
``unsubscribe``, ``email_lists``
    The list each broadcast email is on, its signed links, and leaving it.
``email_domain``
    Whether the sending domain's SPF, DKIM and DMARC are published.

The routes are in :mod:`app.api.routers.auth` and :mod:`app.api.routers.account`;
reading the session off a request is :mod:`app.api.auth`.
"""
