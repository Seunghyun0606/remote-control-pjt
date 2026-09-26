# P3 Reproducible Build & Windows Full CI — 0.13.1

Remote Control 0.13.1 closes the final P3 findings from the production-hardening review.

## 1. Build toolchain ownership

Application dependencies were already version-constrained, but the build frontend/backend path could still drift because CI previously used:

```text
pip install --upgrade pip
pip install -c constraints/lock.txt -e ".[dev]"
```

That left the active pip version, Hatchling version, Hatchling build dependencies, and PEP 517/660 isolated build environment free to resolve differently over time.

0.13.1 makes those inputs explicit.

### Installer bootstrap

`constraints/pip.txt` pins the pip wheel by exact version and SHA-256.

CI installs it with:

```bash
python -m pip install \
  --require-hashes \
  --only-binary=:all: \
  -r constraints/pip.txt
```

### Build backend

`pyproject.toml` now requires:

```toml
[build-system]
requires = ["hatchling==1.27.0"]
build-backend = "hatchling.build"
```

`constraints/build.txt` enumerates the complete Hatchling bootstrap/editable toolchain used by this project:

- hatchling
- editables
- packaging
- pathspec
- pluggy
- trove-classifiers

Every entry is exact-version pinned and SHA-256 locked to a universal wheel.

CI installs these entries with `--no-deps`. This is deliberate: if the build backend starts requiring an additional dependency, CI fails instead of silently resolving an unpinned package.

### No build isolation

The application is then installed with:

```bash
python -m pip install \
  --no-build-isolation \
  -c constraints/lock.txt \
  -e ".[dev]"
```

PEP 517/660 therefore uses the already hash-locked build environment instead of creating a second resolver-controlled environment.

`python -m pip check` runs after installation.

## 2. Lock boundaries

The repository now separates three dependency concerns:

```text
constraints/pip.txt
  installer bootstrap, version + wheel hash

constraints/build.txt
  PEP 517/660 backend/toolchain, version + wheel hash

constraints/lock.txt
  application/runtime/test dependency version snapshot
```

The application lock remains cross-platform version-constrained rather than one binary hash set because platform-specific wheels differ between Linux and Windows. The build-toolchain packages selected here are universal wheels, so their exact artifacts can be hash-locked identically on both platforms.

## 3. Windows full CI

The old Windows job ran a curated list of smoke-test modules. That meant newly added safety boundaries could be covered by Linux full CI but accidentally omitted from Windows.

0.13.1 removes the allowlist.

Windows now runs:

```bash
pytest -q
```

against the complete repository test suite, the same test command used by Linux.

This directly covers, among other boundaries:

- Control API authentication;
- server-owned API identity and spoof rejection;
- cross-channel Approval ownership;
- Controller/Runner singleton and generation ownership;
- process executable/start-token identity;
- migration and schema consistency;
- lifecycle transaction atomicity;
- crash/restart recovery;
- Runner journal result acknowledgement.

More importantly, future tests are included automatically without editing the workflow.

## 4. CI matrix

0.13.1 validates:

```text
Linux / Python 3.11 / full suite
Linux / Python 3.12 / full suite
Windows / Python 3.11 / full suite
```

All three jobs use the same pinned pip/bootstrap/build installation sequence.

## 5. Updating dependencies

Do not change only `pyproject.toml`.

For a build-tool change:

1. choose the exact build package versions;
2. update `pyproject.toml` when the backend version changes;
3. update `constraints/build.txt`;
4. update SHA-256 wheel hashes;
5. update `constraints/pip.txt` if pip changes;
6. run the full CI matrix.

For application dependencies, update `constraints/lock.txt` and keep the existing direct-dependency coverage tests green.

## 6. Compatibility

```text
Package: 0.13.1
Runner Protocol: v3
Database schema: v5
```

No database migration or Runner protocol change is required from 0.13.0.
