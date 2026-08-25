"""ORM models.

Importing this package must import every model module, because
``Base.metadata`` is only populated as a side effect of the class bodies being
executed. Alembic imports this package and nothing else; a model module missing
from the list below is a table autogenerate will never see.
"""

from app.db.models.base import NAMING_CONVENTION, Base

__all__ = ["NAMING_CONVENTION", "Base"]
