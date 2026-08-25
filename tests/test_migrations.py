"""Rules the migration history has to keep obeying.

Schema changes are the one part of this codebase that cannot be fixed forward
by editing a file: a revision that has been applied somewhere is history. So
the properties that make history usable — one line of it, ordered, reversible,
reviewable — are asserted here rather than left to code review to notice.

None of these need a database. They read the version files and env.py as text
and as syntax trees, plus one subprocess that renders the whole chain to SQL
offline. The behaviour against a live Postgres (``upgrade head`` then
``downgrade base``) belongs in the integration suite.
"""

import ast
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.models.base import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ENV_PY = REPO_ROOT / "alembic" / "env.py"
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

# 20260825_0001_baseline.py — the date prefix the file_template in alembic.ini
# produces. Anchored, because "contains eight digits somewhere" would pass on a
# file named after a revision id that happened to be numeric.
FILENAME_RE = re.compile(r"^(?P<date>\d{4})(?P<month>\d{2})(?P<day>\d{2})_(?P<rest>.+)\.py$")


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    """Alembic's own view of the version files.

    ``ScriptDirectory`` parses every revision and resolves the chain without
    connecting to anything — ``revision_environment`` is off, so env.py is not
    run and no DSN is needed.
    """
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def version_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def function_named(module: ast.Module, name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no {name}() defined")


def body_without_docstring(func: ast.FunctionDef) -> list[ast.stmt]:
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


# --- the chain --------------------------------------------------------------


def test_there_is_exactly_one_head(script_directory: ScriptDirectory) -> None:
    """Two heads means two people branched from the same revision, and
    ``upgrade head`` stops with "Multiple head revisions are present" — during a
    deploy, which is the worst time to be reading about merge revisions.
    """
    heads = script_directory.get_heads()
    assert len(heads) == 1, f"branched history, heads: {heads}"


def test_the_chain_is_linear_and_reaches_every_revision(
    script_directory: ScriptDirectory,
) -> None:
    """Walking base -> head must visit every file on disk.

    A revision that is not on the path is one nothing will ever apply: usually a
    file whose ``down_revision`` was left pointing at the wrong parent after a
    rebase, which is invisible until the table it creates is missing in prod.
    """
    walked = {script.revision for script in script_directory.walk_revisions()}
    on_disk = {_revision_id(path) for path in version_files()}

    assert walked == on_disk, f"orphaned revisions: {sorted(on_disk - walked)}"


def _revision_id(path: Path) -> str:
    for node in parse(path).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "revision"
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    raise AssertionError(f"{path.name} declares no revision id")


def test_baseline_is_the_root(script_directory: ScriptDirectory) -> None:
    """0001 is what `alembic stamp` names when adopting an existing database."""
    base = script_directory.get_base()
    assert base == "0001"
    assert script_directory.get_revision("0001").doc == "baseline"


# --- reviewability ----------------------------------------------------------


def test_every_migration_filename_leads_with_a_real_date() -> None:
    """`ls alembic/versions` should read chronologically, and the date should be
    a date — a typo'd month sorts a migration into the wrong year forever."""
    files = version_files()
    assert files, "no migrations found"

    for path in files:
        match = FILENAME_RE.match(path.name)
        assert match, f"{path.name} does not start with YYYYMMDD_ (see file_template)"
        # Raises ValueError on 20261340; that is the assertion.
        date(int(match["date"]), int(match["month"]), int(match["day"]))


def test_filenames_sort_in_chain_order(script_directory: ScriptDirectory) -> None:
    """The date prefix is only useful if it agrees with the actual order. A
    migration committed from a long-lived branch can carry an older date than
    its parent, which makes the directory listing lie about what runs first."""
    by_revision = {_revision_id(path): path.name for path in version_files()}
    chain = [script.revision for script in reversed(list(script_directory.walk_revisions()))]
    names = [by_revision[revision] for revision in chain]

    assert names == sorted(names), f"filename order disagrees with the chain: {names}"


# --- reversibility ----------------------------------------------------------


def test_every_migration_defines_both_directions() -> None:
    for path in version_files():
        module = parse(path)
        function_named(module, "upgrade")
        function_named(module, "downgrade")


def test_downgrade_is_implemented_on_every_migration_after_the_baseline(
    script_directory: ScriptDirectory,
) -> None:
    """The AC, as a test: `pass` is allowed only in the root revision.

    Downgrading past the root means "no schema at all", which an empty baseline
    already leaves you at. Every revision with a parent changed something, so
    every one of them has something to undo — and a migration you cannot reverse
    is a deploy you cannot roll back.
    """
    roots = set(script_directory.get_bases())

    for path in version_files():
        revision = _revision_id(path)
        body = body_without_docstring(function_named(parse(path), "downgrade"))
        is_noop = all(isinstance(statement, ast.Pass) for statement in body)

        if revision in roots:
            continue
        assert not is_noop, (
            f"{path.name}: downgrade() is a bare pass. Write the reverse of "
            f"upgrade(); if it loses data, say so in the docstring and restore "
            f"the structure anyway."
        )


# --- env.py wiring ----------------------------------------------------------


def _configure_calls(module: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "configure"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "context"
    ]


def test_env_targets_the_declarative_base_metadata() -> None:
    """autogenerate compares against `target_metadata`; point it anywhere else
    and it proposes dropping every table it cannot see."""
    module = parse(ENV_PY)
    assignments = [
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "target_metadata" for t in node.targets)
    ]

    assert len(assignments) == 1
    assert ast.unparse(assignments[0].value) == "Base.metadata"


def test_env_imports_the_models_package() -> None:
    """`Base.metadata` is populated as a side effect of the model modules being
    executed. Without this import every table is invisible to autogenerate."""
    source = ENV_PY.read_text(encoding="utf-8")
    assert "import app.db.models" in source


def test_models_package_import_populates_metadata() -> None:
    """The other half: app/db/models/__init__.py must actually import them.

    Trivially true today (no models yet) and the point of the test is that it
    stops being trivially true the moment one is added without being re-exported.
    """
    import app.db.models  # noqa: F401

    for table in Base.metadata.tables.values():
        assert table.schema in (None, "public")


def test_both_configure_calls_compare_types_and_server_defaults() -> None:
    """Off by default, and off is how a varchar(20) -> varchar(40) or an added
    server_default becomes "no changes detected" and a silent schema drift.

    Asserted on *every* configure call, so the offline (`--sql`) path cannot
    quietly render different DDL than the online one.
    """
    calls = _configure_calls(parse(ENV_PY))
    assert len(calls) == 2, "expected one configure() for offline and one for online"

    for call in calls:
        flags = {kw.arg: kw.value for kw in call.keywords}
        for name in ("compare_type", "compare_server_default"):
            assert name in flags, f"configure() is missing {name}"
            flag = flags[name]
            assert isinstance(flag, ast.Constant), f"{name} is not a literal"
            assert flag.value is True, f"{name} is {flag.value!r}, not True"


def test_the_ini_carries_no_database_url() -> None:
    """A URL in the ini is a second source of truth for "which database", and a
    committed password. env.py sets it from Settings."""
    assert not Config(str(ALEMBIC_INI)).get_main_option("sqlalchemy.url")


# --- the whole thing, rendered ----------------------------------------------


def test_the_chain_renders_to_sql_offline() -> None:
    """Runs env.py for real — Settings wiring, imports, the lot — and applies
    every migration in offline mode.

    No database: `--sql` emits DDL to stdout rather than executing it, which is
    also how a migration gets reviewed before a production window. A broken
    import or an unset variable in env.py fails here rather than in a deploy.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # conftest has already put the two required variables in os.environ;
        # POSTGRES_* stay at their defaults because nothing connects.
        env=os.environ,
    )

    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE alembic_version" in result.stdout
    assert "INSERT INTO alembic_version" in result.stdout


def test_downgrade_to_base_renders_to_sql_offline(script_directory: ScriptDirectory) -> None:
    """The reverse direction, which is the one nobody runs until they need it.

    Offline downgrade needs an explicit starting revision — there is no database
    to ask — so this also proves the head can be named without one.
    """
    head = script_directory.get_current_head()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", f"{head}:base", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=os.environ,
    )

    assert result.returncode == 0, result.stderr
    assert "DELETE FROM alembic_version" in result.stdout
