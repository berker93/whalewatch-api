"""The derived layer: tables rebuilt from the normalised ones, never patched.

``docs/data-model.md`` calls this layer a cache with a schema. Every table in it
is rebuilt from ``filing`` and ``holding`` by ``whalewatch recompute``, and
nothing may depend on one for correctness. A rebuild is the only way a row here
changes. An amendment that lands a year late changes a past period, and an
incremental update would have to find every row downstream of it.
"""
