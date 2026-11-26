Testing
=======

This library uses `pytest` for testing.
If installed properly via `requirements-dev.txt`, just running `pytest` will execute all tests.
(This is also done automatically as part of the lint script.)
Run `pytest -h` or check the PyTest docs for help.

When prudent, add new tests either to one of the existing test scripts under `tests/`,
or a new test script.

Coding Conventions
==================

Python Version & Dependencies
-----------------------------

* Target **Python 3.12+** (use modern features such as `from __future__ import annotations`, PEP 695 `type` aliases, `@dataclass(slots=True)`, and `collections.abc`).
* Keep runtime dependencies focused on release tooling (`build`, `check-manifest`, `twine`) and config parsing (`kdl-py`) plus the Python standard library.
* Development has several dependencies, tracked in `requirements-dev.txt`, needed for linting and testing. These can be installed with `pip install -r requirements-dev.txt`, ideally in a virtual environment (such as with `pyenv` or `uv`).

Naming
------

* Use `camelCase` for functions, methods, variables, parameters, and attributes/fields (e.g. `loadConfig`, `bumpSemver`).
* Use `PascalCase` for classes and type aliases (e.g. `TablishConfig`, `ReleaseOptions`).
* Use `UPPER_SNAKE_CASE` for module-level constants (e.g. `DEFAULT_TEST_PYPI_URL`, `DEFAULT_SEMVER_FILE`).
* Prefix internal helpers, caches, and private types with a leading underscore (`_parseLintCommand`, `_emit`).
* Test functions under `tests/` use pytest's `test_snake_case` naming convention.

Style, Formatting & Linting
---------------------------

All code across the repository (`tablish/` and `tests/`) must pass `./lint` cleanly (`ruff`, `black`, `mypy`, `pylint` at 10/10, and `check-manifest`):

* **Formatting:** `black` with a **120-character** line length, and trailing commas on multiline calls/collections (`COM812`).
* **String concatenation:** Always use explicit `+` across lines rather than implicit multiline string concatenation (`ISC002`).
* **Exceptions:** Assign exception message strings/f-strings to a local variable (`msg = ...`) before raising (`EM101`/`EM102`), and chain causes explicitly (`from exc` / `from None`).
* **No `print()` in library modules:** CLI I/O belongs in `tablish/cli.py` (`T201`).
* **Imports:** Sorted via `ruff` (`I001`) with `combine-as-imports = true`. Public re-exports in `tablish/__init__.py` use explicit `X as X` aliases.
* **No drive-by lint disables:** Fix lint warnings properly rather than suppressing them, unless a narrow inline comment is genuinely unavoidable.

Typing & Patterns
-----------------

* **Full type annotations:** All functions and methods—including tests—must be fully typed (`mypy` runs with `disallow_untyped_defs`, `no_implicit_optional`, `warn_redundant_casts`, `warn_unused_ignores`, and `warn_return_any`).
* **Centralized typing module:** Keep shared type aliases and re-exports in `tablish/t.py` (imported as `from . import t`).
* **Lightweight objects:** Use `@dataclass(frozen=True, slots=True)` for configs, options, and results.
