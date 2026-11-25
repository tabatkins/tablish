# tablish

A typed Python 3.12+ CLI tool for releasing Python packages to PyPI, configured via `[tool.tablish]` in `pyproject.toml`.

## What it does

1. Verifies you are at the root of the expected git repository with a clean working tree.
2. Reads and bumps your semver file (`break`/`major`, `feature`/`minor`, `bugfix`/`patch`, or an explicit `MAJOR.MINOR.PATCH` version).
3. Runs your project's lint/test command (or `check-manifest` when `--skip-lint` is passed) and verifies it leaves the working tree clean.
4. Optionally generates extra version/metadata files (and rolls them back automatically on `--dry-run`, `--test`, or failure).
5. Builds sdist and wheel with `build` and validates them with `twine check`.
6. Uploads to PyPI (or `test.pypi.org` with `--test`) using a token from `secrets.json` or `TWINE_PASSWORD`.
7. Commits the updated version file(s), creates an annotated git tag, and pushes with `--follow-tags`.

## Installation

```sh
pip install tablish
```

Or install locally in editable mode:

```sh
pip install -e /path/to/tablish
```

Requires Python 3.12+.

## Usage

Run `tablish` from your project's root directory (or pass `-C PATH`):

```sh
tablish bugfix                  # bump patch version (or `patch`) and publish to pypi.org
tablish feature --dry-run       # bump minor version (or `minor`) without uploading or committing
tablish break --test            # bump major version (or `major`), upload to test.pypi.org, and roll back
tablish 2.0.0                   # release an explicit version
tablish bugfix --skip-lint      # skip lint command (still runs check-manifest if enabled)
tablish bugfix --no-tag         # skip creating/pushing a git tag
tablish bugfix --verbose-pypi   # show verbose output and error details from PyPI upload
tablish info                    # display resolved publish metadata (explicit vs defaulted)
```

## PyPI Credentials

By default, `tablish` reads a JSON file at `secrets.json` (gitignored!) in your project root:

```json
{
  "pypi.org release key": "pypi-...",
  "test.pypi.org release key": "pypi-..."
}
```

If the secrets file is inside the repository (including the default `secrets.json`), `tablish` verifies that it is untracked and gitignored and refuses to run if it is not. If the secrets file is absent, `tablish` falls back to the `TWINE_PASSWORD` environment variable (and `TWINE_USERNAME`, defaulting to `__token__`). You can also point `secrets-file` to a shared path outside the repository such as `~/.config/tablish/secrets.json`.

## Configuration (`pyproject.toml`)

All keys under `[tool.tablish]` are optional; sensible defaults are inferred from `pyproject.toml`.

```toml
[tool.tablish]
project-name = "myproject"          # defaults to [project].name (set "" to disable remote/dir match)
semver-file = "semver.txt"          # defaults to [tool.setuptools.dynamic].version.file or "semver.txt"
secrets-file = "secrets.json"       # relative to project root, or absolute / ~-expanded path
pypi-secret-key = "pypi.org release key"
test-pypi-secret-key = "test.pypi.org release key"
pypi-url = ""                       # optional custom repository URL for production uploads
test-pypi-url = "https://test.pypi.org/legacy/"
build-dirs = ["build", "dist"]
lint-command = ["sh", "lint"]       # list of args, shell string, or false to disable (defaults to ./lint)
check-manifest = true               # run check-manifest when lint is skipped or disabled
check-manifest-args = ["--no-build-isolation"]
tag = true                          # create and push an annotated git tag (overridden by --no-tag)
tag-format = "v{version}"
tag-message = "Release {version}"
commit-message = "Bump semver to {version}"
```

### Extra generated files

Projects that also maintain generated version/build info files can declare `[[tool.tablish.generated-files]]` entries. Each entry takes a relative `path` and either a `command` (whose stripped stdout is written to `path`) or a `template` string (with `{version}` replaced):

```toml
[[tool.tablish.generated-files]]
path = "mypkg/version-info.txt"
command = ["git", "log", "-1", "--format=mypkg version %h, updated %cd"]

[[tool.tablish.generated-files]]
path = "mypkg/_version.py"
template = '__version__ = "{version}"\n'
```

Generated files are written alongside `semver-file` before building, staged and committed on real releases, and automatically rolled back (or deleted if newly created) on `--dry-run`, `--test`, or errors.

## Development

```sh
pip install -r requirements-dev.txt
./lint      # runs ruff, black, mypy, pylint (10/10), check-manifest, and pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for coding conventions.

## License

MIT License — see [LICENSE](LICENSE).
