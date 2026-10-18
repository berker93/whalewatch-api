"""Accounts: who is signed in, and the rules their names, emails and passwords follow.

``passwords``
    scrypt hashing, in a thread.
``sessions``
    The session cookie's token and the emailed links' tokens, hashed at rest.
``rules``
    What a name, an email, a password and a time zone must be: the frontend's
    rules or looser.
``mail``
    The account emails, logged in development and sent by SMTP where deployed.

The routes are in :mod:`app.api.routers.auth` and :mod:`app.api.routers.account`;
reading the session off a request is :mod:`app.api.auth`.
"""
