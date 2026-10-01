"""The derived layer: tables rebuilt from the normalised ones, never patched.

``docs/data-model.md`` calls this layer a cache with a schema. Every table in it
is rebuilt from ``filing`` and ``holding`` by
:func:`~app.derived.recompute.recompute`, and nothing may depend on one for
correctness. A rebuild is the only way a row here changes. It covers a
:class:`~app.derived.scope.Scope` of ``(filer, period)`` pairs, deleted and
inserted again, and the changes of the period after each, which compare against
it. An amendment that lands a year late rebuilds its own period and the next,
and nothing downstream has to be found and patched.
"""
