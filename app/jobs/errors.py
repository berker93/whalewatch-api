"""The one failure a job raises on purpose."""


class JobError(Exception):
    """An operational failure with a message worth reading and no traceback.

    Something a person can act on — a filing that is not what it claims, one
    loaded before its documents were archived — rather than a bug. The CLI
    prints it as one line on stderr; a task fails with it, unretried
    (app/jobs/retry.py), since nothing about it changes with time.
    """
