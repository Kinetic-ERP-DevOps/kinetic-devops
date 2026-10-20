# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- The package now lives in `src/kinetic_devops/` (src layout; moved with `git mv`, so `git log --follow`
  keeps each file's history). Packaging finds it via `where = ["src"]`.
- No more `sys.path` / `PYTHONPATH` tweaks in scripts, tests or `env_init`: the package is imported from
  the environment `uv sync` installs, so run things with `uv run`. The `env_init` wrappers default to
  uv's `.venv`; a worktree hook run with `KINETIC_HOOK_PYTHON` puts that worktree's `src/` first.
- CI (GitHub and Forgejo) installs with `uv sync --locked` and runs everything through `uv run`.
- This completes the src-layout migration; its roadmap and tracker documents are removed.

### Added
- Native checks (tests that execute uv's generated console-script `.exe` launchers) are deferred in
  local runs and the pre-commit hook, because enterprise endpoint security may
  block newly built unsigned files. They run in CI, in Windows Sandbox, or on request with
  `tests.test_runner --native` / `KINETIC_NATIVE_CHECKS=1`.
- GitHub CI job "Windows Test Gate" on `windows-latest`: the test suite plus
  `scripts/windows_smoke.ps1` (console-script launchers, `env_init.ps1` / `env_init.bat`,
  `install-hook.ps1`) under Windows PowerShell 5.1.
- `scripts/sandbox_test.py`: tests a commit on a clean Windows in Windows Sandbox: the full suite plus
  the Windows smoke checks, or `--native-only`, `--tests NAME ...`, `--smoke-only`. With `wsb` it
  keeps one sandbox running and reuses it (adopting a running sandbox it doesn't know via
  `wsb share`), so later runs skip boot and uv setup. Git there is MinGit on the sandbox's own disk,
  from a version- and checksum-checked cache on the host (`--refresh-mingit` ignores the cache,
  `--no-mingit` uses the host's Git). An optional
  pre-push hook runs the full check when enabled with `git config kinetic.prepushSandbox true`;
  commits that already passed are skipped.
- Environment comparison (informative only): every CI test job records its run as JUnit XML plus
  metadata and uploads it; `scripts/run_timings.py` records this machine, the kept Windows Sandbox,
  WSL (newest Ubuntu with uv) and a `python:3.14-alpine` container via Podman, with `--repeat N` for
  medians, flaky-test detection and a busy-machine flag; `scripts/compare_timings.py report` shows totals (suite and end-to-end, with each run's scope
  and where scopes differ), tests not run everywhere with skip reasons, environment-only failures,
  and like-for-like per-module/per-test times. CI now runs the suite via `python -m pytest` (same
  tests as `tests.test_runner`).
- CI job "Built Wheel Smoke Test": builds the sdist and wheel, checks the wheel contains only
  `kinetic_devops/`, installs it into a clean environment and, from outside the checkout, imports every
  module and starts each console script and module CLI.

### Fixed
- Encrypted-vault tests never ran on Windows: `cryptography` was only present where `keyring` pulls
  it in (Linux). It is now a dev dependency and a user-facing extra (`kinetic-devops[vault]`). When
  it is installed but can't load -- e.g. endpoint security blocks its native library -- the error
  says so (with the cause) instead of claiming it is missing. The vault test is a native check
  (it loads a native library): deferred in local runs, run in CI, Windows Sandbox and on request.
- Removed `[tool.uv] compile-bytecode` (added in 0.1.0a7): it made every `uv run` recompile all
  installed packages (~4 s each time), and on machines with endpoint security those cache writes
  intermittently failed ("user-mapped section open", os error 1224). Python caches bytecode on first
  import anyway.
- `scripts/env_init.bat` was stored with LF-only line endings, which `cmd.exe` misparses;
  `.gitattributes` now checks out `*.bat` / `*.cmd` with CRLF.
- The branch-protection dry-run test no longer depends on the checkout's `origin` remote.
- `auth ai-set` accepts only absolute http(s) AI base URLs with a host; embedded credentials, query
  strings and fragments are rejected. A failed `--ai-profile` selection keeps the previous profile.

## [0.1.0a7] - 2026-10-13

### Added
- EFx `plan` / `deploy`: build the package an environment repository declares (per-environment
  deployment record + source ref), show how it differs from the live library and any drift, and deploy
  it with `ImportLibrary` (OverwriteMode=1) -- idempotent, snapshot before, verify after, record the
  result. Rollback is a deploy of an earlier ref.
- `test-pilot --allow-env`: run a saved function in an explicitly named environment; picks the staging
  or published endpoint from the library's state.
- `auth rename OLD NEW [--dry-run]`: rename a saved environment without re-entering credentials
  (config, cached sessions and the last-session pointer move; encrypted vaults stay encrypted).
- AI server profiles in the OS keyring: `auth ai-set --name NAME` / `ai-show` / `ai-default` /
  `ai-clear` store named profiles (URL, API style `openwebui` | `openai`, default model) with the API key
  kept once per server URL, never on the command line. `analysis --ai-profile NAME` selects one;
  Open WebUI's `/api/models` + `/api/chat/completions` and bearer keys are supported.
- Sensitive-data scanner: configured environment nicknames are reported as non-failing notices.
- Every CLI explains its subcommands and arguments in `-h`; commands that write to Epicor, delete data
  or print secrets are labelled.
- Test/hook opt-ins for restricted workstations: `KINETIC_HOOK_PYTHON` (run hook checks with an existing
  interpreter instead of building a new environment), `KINETIC_SKIP_CONSOLE_SCRIPT_TESTS`,
  `KINETIC_TEST_TIMEOUT_SCALE`, `KINETIC_TEST_TMPDIR`.
- `kinetic_devops/efx_library.py`: EFx Function Library management (`Ice.Lib.EfxLibraryDesignerSvc`) --
  `list`/`export`/`export-all`/`import` CLI subcommands, verified live against a real environment.
- Per-function C# source extraction and push-back: `pull-source` extracts each function's embedded
  `CustomCodeAction.Code` fragments into individual `.cs` files; `push-source` re-injects edited
  fragments back via `ApplyChangesWithDiagnostics`. Verified live end-to-end (extract -> edit -> push
  -> confirm -> revert) against a test environment.
- `test-pilot` subcommand: explicit, test-environment-only, interactive-confirmation-required live execution of a
  saved function (refuses to run unattended).
- `tools/efx_syntax_check`: a Roslyn-based syntax-only validator for extracted EFx function C#, plus a
  CI job that builds it on both GitHub and Forgejo.
- `CONTRIBUTING.md`: coverage-matrix registration requirements, the current service-module pattern,
  the live-verification-before-documenting principle, and the dual-remote (Forgejo/GitHub) push
  convention.

### Changed
- `--help` is much faster: `keyring` and `requests` load on first use; dependencies are pre-compiled at
  install (`[tool.uv] compile-bytecode`).
- `repomaker` uses argparse for its top-level command and help (behavior unchanged).
- `analysis`: the AI server comes from `--ai-base-url`, then the stored profile, then the
  `KINETIC_AI_BASE_URL` fallback.
- `trim_trace_paths`: site-specific redaction terms (company IDs, plant patterns, names, employee IDs,
  usernames) load from a local file (`$KINETIC_TRACE_REDACTIONS` or
  `~/.kinetic-devops/trace_redactions.json`) instead of the code.
- Repository-management scripts require `-Repo` instead of defaulting to an account.
- Dependency updates: `requests` 2.34.2, `urllib3` 2.8.0.
- Dependency updates: integrated Dependabot upgrade for `urllib3` from `2.6.3` to `2.7.0` via `uv.lock`.
- Project runtime requirement now targets Python `3.10+` (from `3.8+`) in package metadata and README.
- `Documents/ARCHITECTURE.md`: corrected its service-module example, which documented a
  `boreader_service.py`/`BOReaderService(base_url, headers)` pattern that no longer matches reality --
  every current service module subclasses `KineticBaseClient` directly.

### Fixed
- Pre-commit hook: the hook installer resolves the real hooks directory (works from linked worktrees);
  the test suite runs without git's repository environment variables, which in linked worktrees made
  tests operate on the real repository.
- `github.com` host checks require a dot boundary in the host suffix.
- `KineticCore`: a clear error when no keyring backend is available.

### Security
- This release starts a new public history: organization-specific references were removed from the
  code, documentation and tests. Earlier releases (0.1.0a1-0.1.0a6) are superseded.

## [0.1.0a6] - 2026-09-09

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

### Added
- Native Access Scope `.eas` export support and `.eas` artifact normalization for comparison against live environments.
- Blocking sensitive-data gating in the local pre-commit hook and CI workflow, including remote-history scanning support.
- User-adjustable generic Base64 risk profile flags to opt into CamelCase, snake_case, dash-separated, path-like, or alpha-only candidate detection.

### Changed
- Access Scope validation and refresh/import workflows now accept normalized `.eas` packages directly as reference artifacts.
- Generic Base64 scanning now requires a successful decode and suppresses common identifier-shaped false positives by default.

### Fixed
- Access Scope migrate and refresh-import key verification now use company-qualified API key lookups.
- API key rebind operations now refresh current rows before `UpdateExt`, preventing stale `SysRevID` no-op failures.
- Commit and CI gating now fail when sensitive-data findings are detected instead of only printing advisory output.
## [0.1.0a5] - 2026-04-12

**Status: ⚠️ ALPHA** — Early development.

### Changed
- Refactored and consolidated authentication utilities into `kinetic_devops.auth` to improve credential lifecycle management.
- Standardized auth-related error handling across `KineticBaseClient` and service modules.
- Hardened Solution Workbench and MetaUI layer logic for more reproducible migration behavior.

## [0.1.0a4] - 2026-04-10

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

### Added
- Post-publish release validation workflow that runs after package publish, verifies the released version is visible on PyPI, and uses native GitHub workflow failure signaling for notifications.

### Fixed
- CI and release dry-run behavior now skip token resolution for branch-protection smoke when `--apply` is not enabled.
- Forgejo dry-run in hosted CI no longer requires an explicit Forgejo API base URL when provider context is inferred.
- Solutions module compatibility was hardened for Python 3.10/3.12 execution environments.

## [0.1.0a3] - 2026-04-10

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

### Added
- `solutions install` support for `--auto-clear-layer-conflicts` to detect MetaUI "already exists in another company" failures, delete tenant-scoped layer rows, and retry once automatically.
- Regression tests for Solution helper conflict parsing behavior (identifier and alternate warning formats).

### Changed
- Refactored `repomaker apply` implementation into package engine modules for cleaner dispatch and maintainability.
- Solution recreate/install hardening for target ID normalization and safer existence handling in environments that return `GetByID` 400 on missing records.

### Fixed
- Prevented false-success recreate outcomes by validating post-recreate solution membership and failing when source had members but target persisted none.
- Resolved tenant layer cleanup path by using `MetaFX DeleteLayer` fallback when `BulkDeleteLayers` is rejected by tenant/service behavior.

## [0.1.0a2] - 2026-04-02

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

### Added
- Console script entry point for direct CLI invocation: `kinetic-devops`
- Regression tests for CLI entry routing and submodule warning behavior
- Definition of Done release gates for regression testing and practical CI/CD controls

### Changed
- Refactored `kinetic_devops.__main__` to cleanly dispatch subcommands via direct function calls
- Updated package imports to lazy loading to prevent submodule preload side effects

### Fixed
- Removed RuntimeWarning during `python -m kinetic_devops.<module>` invocation in normal CLI help flow

## [0.1.0a1] - 2026-03-03

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

### Added
- **Core Authentication** — Secure keyring-backed credential management with optional PBKDF2+AES-256 encryption
- **Multi-environment Support** — Switch seamlessly between Dev/Test/Production environments
- **Service Clients**:
  - `KineticBAQService` — Execute Business Analysis Queries
  - `KineticBOReaderService` — Query Business Objects via BOReader
  - `KineticFileService` — Manage DMS storage configurations
  - `KineticReportService` — Upload and manage RDL reports
  - `KineticTaxService` — Manage tax configurations
  - `KineticMetafetcher` — Query system metadata
- **Base Infrastructure**:
  - `KineticBaseClient` — Session management and authenticated requests
  - `KineticConfigManager` — Credential storage and token lifecycle
  - `KineticCore` — Security, redaction, and wire logging
- **CLI Tools**:
  - `env_init.py` / `env_init.ps1` — Interactive environment setup
  - `validate.py` — Environment health checks
  - `pull_tax_configs.py` — Export tax configurations
  - `refresh_post_db.py` — Post-database refresh automation
  - `tax_clear.py` — Bulk tax record deletion
- **Security Features**:
  - Heuristic-based sensitive data redaction
  - Wire logging with request/response masking
  - Secure configuration hashing and integrity verification
- **Documentation**:
  - Architecture guide with design patterns
  - Helper script reference
  - Test suite documentation

### Known Issues
- **Redaction edge cases** — Heuristic regex has known gaps with escaped JSON and whitespace variations
- **Windows-first development** — Linux/macOS tested but less mature
- **Pre-commit hook** — May fail on slow systems during initial test run

### Security Notice
- **Never commit `.env` or keyring credentials** — Already excluded in `.gitignore`
- **Alpha APIs may change** — Especially around request/response signatures
- **Test environment only** — Not recommended for production yet

### Future (Post-Alpha)
- Enhanced redaction with configurable patterns
- Async service client support
- GraphQL support for complex BOReader queries
- Docker/container environment initialization
- GitHub Actions integration templates
