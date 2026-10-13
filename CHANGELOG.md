# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
- Dependency updates: integrated Dependabot upgrade for `urllib3` from `2.6.3` to `2.7.0` via `uv.lock`.
- Project runtime requirement now targets Python `3.10+` (from `3.8+`) in package metadata and README.
- `Documents/ARCHITECTURE.md`: corrected its service-module example, which documented a
  `boreader_service.py`/`BOReaderService(base_url, headers)` pattern that no longer matches reality --
  every current service module subclasses `KineticBaseClient` directly.

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
