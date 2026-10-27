"""The admin console: the people who run WhaleWatch, and what they do to accounts.

``auth``
    Admins' own accounts and sessions: not a flag on a user. Made from the CLI.
``log``
    ``admin_action``, append-only: every sign-in and every change an admin makes.
``users``
    The user list and one user's page: plan, subscription, override, usage.
``plans``
    Plan overrides (a reason and an expiry), and grandfathering a subscription
    to a snapshot of its plan.
``metrics``
    Signups, actives and plan mix by day: the daily snapshot and the series.

Payments are not here: Paddle's dashboard has them. The routes are
:mod:`app.api.routers.admin`; reading the admin session off a request is
:mod:`app.api.admin_auth`.
"""
