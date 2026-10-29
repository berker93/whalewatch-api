"""Alerts fired from the pipeline: what a rebuild published, told to whoever asked.

Three steps, in three places, so that ingestion never waits on alerts and
never fails because of them:

1. :mod:`app.alerts.dispatch`: ``recompute`` names the ``(filer, period)``
   pairs it rebuilt, and once its transaction commits they are handed to the
   ``emit_alerts`` task. A rebuild that rolls back hands over nothing.
2. :mod:`app.alerts.events`: the task reads what happened in those pairs from
   the derived tables, as they are now: a 13F published, and each position it
   opened, added to, trimmed or exited, split-adjusted.
3. :mod:`app.alerts.firing`: each event against the rules that ask for it,
   written as notifications, and then emailed where a rule says so.

Every step can run twice. A notification's ``dedupe_key`` names the event, so
a second emission of it writes nothing and sends nothing.
"""
