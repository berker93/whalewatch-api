"""Celery task definitions.

Each task's body runs inside :func:`app.jobs.tracking.track_run`, exactly as
each CLI verb's does, so a scheduled run leaves the same ``ingestion_run`` row
as one started by hand.
"""
