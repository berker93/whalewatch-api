"""Structured logging configuration.

A backfill of 2,000 filings that dies on number 1,347 is only debuggable if you
can ask the logs a question. ``grep 0001234567-24-000123`` has to return every
line that touched that filing — the fetch, the parse, the retry, the traceback —
and free text cannot promise that, because the accession number appears in a
different position, spelling and sentence in every message that mentions it.
So nothing here formats values into prose. Events are short stable names and the
data travels as key/value pairs::

    log.info("filing.parsed", accession_no=acc, cik=cik, rows=len(holdings))

which renders as one JSON object per line in production, and as an aligned,
coloured line locally where a human, not a log aggregator, is reading it.

Log vocabulary
--------------
Queryability comes from *consistent keys*, not from any one call site being
clever. These names are the project's vocabulary; use them and add to them, but
never spell one of them a second way (no ``accession``, no ``accessionNumber``):

==================  ==========================================================
``accession_no``    EDGAR accession number, dashed form: ``0001234567-24-000123``
``cik``             Central Index Key, zero-padded 10-char string, never an int
``filer_slug``      Our stable slug for a filer, e.g. ``berkshire-hathaway``
``period``          Reporting period the data belongs to, ``YYYY-MM-DD``
``job_name``        Name of the batch job, e.g. ``backfill_13f``
``run_id``          One execution of a job; every line from that run shares it
``request_id``      One HTTP request; bound by the middleware, see below
==================  ==========================================================

``request_id`` and ``run_id`` are the two join keys: given either, the whole
story of one request or one run is a single grep.

Binding context
---------------
Values that describe *everything* an operation does should be bound once rather
than repeated on each call::

    structlog.contextvars.bind_contextvars(job_name="backfill_13f", run_id=run_id)

``contextvars`` and not thread-locals, because this codebase is async: a
thread-local is shared by every coroutine the event loop interleaves on that
thread, so two concurrent requests would overwrite each other's ``request_id``.
A ``ContextVar`` is copied into each task at creation and mutated independently,
which survives ``await``.

Where the output goes
---------------------
Everything, including the standard library's own loggers, is rendered by a
single :class:`structlog.stdlib.ProcessorFormatter` on one handler on the root
logger. That is what makes uvicorn's startup lines and SQLAlchemy's SQL come out
in the same shape as our own events, rather than as a second, differently
formatted stream that whatever ships these logs has to parse twice.
"""

import logging
import sys
from typing import Final, TextIO

import structlog
from structlog.typing import Processor

from app.core.config import Settings

#: Loggers that other libraries attach their own handlers to. Left alone, each
#: would emit its own differently-formatted copy alongside ours.
_THIRD_PARTY_LOGGERS: Final = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "gunicorn.error",
    "sqlalchemy",
    "sqlalchemy.engine",
    "alembic",
    "celery",
    "httpx",
)

#: Environments whose logs are shipped to an aggregator rather than read by a
#: person. Staging is included on purpose: it is where you find out that a field
#: you added does not survive the log pipeline, and it can only tell you that if
#: it is rendered the same way production is.
_JSON_ENVIRONMENTS: Final = frozenset({"staging", "production"})


def _processor_chain() -> list[Processor]:
    """The processors every event passes through before rendering.

    Shared verbatim by structlog's own loggers and by the ``foreign_pre_chain``
    that adapts standard library records, so a uvicorn warning arrives carrying
    the same ``timestamp``, ``level`` and bound ``request_id`` as ours.

    Deliberately identical in every environment. The renderer is the *only*
    thing that varies, and it lives on the handler rather than in this chain,
    which matters because ``cache_logger_on_first_use`` bakes this list into
    each logger the first time it is used: were the chain to differ per
    environment, a logger first used before ``configure_logging`` ran would keep
    the wrong set of fields for the life of the process. Fields are fixed here;
    only their presentation is decided later.
    """
    return [
        # First, so that everything bound for this request/run is already in the
        # event dict when the processors below (and the renderer) see it.
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        # ISO-8601 in UTC. Not the local timezone: the only thing worse than no
        # timestamp is two services' timestamps that cannot be interleaved.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Renders the ``exc_info`` tuple down to an ``exception`` string. Not
        # optional: JSONRenderer cannot serialize a traceback object, so without
        # this a logger.exception() call in production would lose the traceback
        # entirely. ConsoleRenderer would rather format exceptions itself, and
        # gives up its colouring by being handed a finished string — a fair
        # price for local output that carries exactly the fields production does.
        structlog.processors.format_exc_info,
    ]


def _build_renderer(*, json_output: bool, stream: TextIO) -> Processor:
    """Pick the final processor: machine-readable or human-readable."""
    if json_output:
        return structlog.processors.JSONRenderer()
    # Colour is decided by the stream, not the environment. Running locally with
    # output piped to a file or captured by pytest should produce plain text;
    # escape codes are only legible on a terminal.
    return structlog.dev.ConsoleRenderer(colors=stream.isatty())


def configure_logging(settings: Settings, *, stream: TextIO | None = None) -> None:
    """Install the process-wide logging configuration described by ``settings``.

    Idempotent: it replaces the root logger's handlers rather than adding to
    them, so calling it twice — a test building a second app, uvicorn importing
    the module after having configured its own logging — leaves exactly one
    handler and therefore one line per event.

    This is unavoidably global state, which is why it is a free function taking
    settings rather than something ``create_app`` hides. In a process serving
    two apps the last call wins; in production there is only ever one.
    """
    stream = stream if stream is not None else sys.stdout
    json_output = settings.environment in _JSON_ENVIRONMENTS
    level = logging.getLevelNamesMapping()[settings.log_level]

    shared = _processor_chain()
    renderer = _build_renderer(json_output=json_output, stream=stream)

    structlog.configure(
        processors=[
            *shared,
            # Not a renderer: it hands the event dict to the stdlib logger so
            # the ProcessorFormatter below does the actual rendering. This is
            # what puts our events and the stdlib's on one code path.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Safe because the level is the only thing baked into the cached logger,
        # and the level does not change while the process runs. Note the
        # consequence for tests: a logger already used keeps the level it was
        # first configured with, even after a later configure_logging call.
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # remove_processors_meta strips the bookkeeping keys ProcessorFormatter
            # adds (``_record``, ``_from_structlog``) so they never reach output.
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            # Applied only to records from libraries that know nothing about
            # structlog, bringing them up to the same shape as our events.
            foreign_pre_chain=shared,
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        third_party = logging.getLogger(name)
        # Clear, then propagate: the library's records still reach our root
        # handler, they just stop carrying the library's own formatting there.
        third_party.handlers.clear()
        third_party.propagate = True

    # uvicorn's access log is switched off rather than reformatted. The
    # middleware in app.api.middleware emits one access line per request that
    # carries the request_id and the duration; leaving uvicorn's enabled would
    # print a second, poorer line for the same request.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, defaulting to the calling module's name.

    A thin re-export of ``structlog.get_logger`` so call sites depend on this
    module rather than on structlog directly — which is what will make it
    cheap to add a default binding (service name, pod) later, in one place.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
