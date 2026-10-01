"""When each materialised view was last refreshed, so an aggregate can say how old it is.

A materialised view is as of its last refresh, and Postgres does not record
when that was. :func:`~app.derived.views.refresh_views` records it here, in the
transaction that refreshes the view. The row and the view's new rows become
visible at the same commit, so a reader never sees a refresh time newer than
the data it describes.

One row per view, replaced on every refresh. The history is in
``ingestion_run``: each ``refresh-views`` run records, in its ``metrics``, every
view it refreshed and how long each one took. :attr:`MatviewRefresh.run_id`
names that run.

A view with no row has not been refreshed since this table was created
(``0015``). It was filled once before that, by ``0014``, but nothing recorded
when. The API reports that as unknown, not as a guess.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class MatviewRefresh(Base):
    """One materialised view's last refresh."""

    __tablename__ = "matview_refresh"

    view_name: Mapped[str] = mapped_column(Text, primary_key=True)
    """The view's name, as ``pg_matviews.matviewname`` has it. Not a foreign
    key, since a materialised view has nothing for one to point at."""

    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When the refresh finished: the time the view's rows are as of.

    From ``clock_timestamp()``. ``now()`` is when the transaction began, and in
    a refresh of several views it would give every view the start time of the
    first one.
    """

    run_id: Mapped[uuid.UUID | None] = mapped_column()
    """The ``refresh-views`` run that did it. Its ``ingestion_run`` row has how
    long the refresh took, and its log lines carry this id.

    Null only for a refresh made outside a tracked run, which only tests do.
    Not a foreign key: nothing depends on ``ingestion_run`` for correctness,
    and this row is what the API serves.
    """
