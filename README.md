# Kinetic DevOps

**Status: ⚠️ ALPHA** — Early development. API and features may change significantly.

**Kinetic DevOps** is a Python SDK and automation framework designed to bring modern DevOps practices to the Epicor Kinetic ecosystem. It provides a secure, extensible, and consistent foundation for environment management, CI/CD pipelines, and complex data migration workflows.

## Features

- 🔐 **Secure credential management** — OS keyring-backed encryption with optional Zero-Knowledge Vault mode and integrated maintenance tools (`diagnose`, `find-orphans`, `full-wipe`).
-  **Multi-environment support** — Seamlessly switch between Dev/Test/Production
- 🛠️ **Ready-to-use Service Clients** — High-level clients for BAQ, BOReader, Reports, Tax, and more, built on a robust base client with wire logging and data redaction.
- 🚀 **Extensible Project Templates** — A "batteries-included" project structure with auto-discovery for creating your own reusable functions, layers, and scheduled jobs.
- 🤖 **CI/CD Ready** — Designed for automation with environment variable support, programmatic APIs, blocking sensitive-data gates, and pre-commit hooks for local validation.
- 📜 **Powerful CLI Tools** — Includes helper scripts for environment initialization and common administrative tasks like configuration syncing. Unified test runner includes built-in health validation.
- 🧩 **Layer Lifecycle Operations** — Native MetaFX support for core layer import/delete operations (`ImportLayers` / `BulkDeleteLayers`) with structured error reports.
- ⚙️ **EFx Function Library Management** — Export/import/list libraries, plus per-function C# source extraction and push-back for local editing.
- 📦 **Modular architecture** — Extend with custom project submodules

## Quick Start

### Windows PowerShell
```powershell
.\scripts\env_init.ps1
uv run kinetic_devops auth store
uv run python -m tests.test_runner --validate
```

### Linux/macOS
```bash
uv run python scripts/env_init.py
uv run python -m kinetic_devops.auth store
uv run python -m tests.test_runner --validate
```

## Usage

```python
from kinetic_devops.auth import KineticConfigManager
from kinetic_devops.baq import BAQClient

# Initialize authentication
config = KineticConfigManager()

# Create BAQ client (handles session, headers, requests)
baq_client = BAQClient(config)

# Execute a BAQ
results = baq_client.execute_baq(
    baq_id='CUST_List',
    args={'Company': 'ABC', 'MaxResults': 100}
)

# Process results
for customer in results['value']:
    print(f"{customer['CustomerID']}: {customer['CustomerName']}")
```

### Local Development (.venv)

Use the repository-local virtual environment at `.venv`:

```powershell
# From repo root
.\.venv\Scripts\activate
uv sync
```

### Analysis Commands (Feature Slice)

The `analysis` router includes three workflows:

```powershell
# Metadata-only functionality/regression review
uv run python -m kinetic_devops analysis no-commit-review

# Incremental snapshot + commit AI review
uv run python -m kinetic_devops analysis diff-review

# Legacy repository intake loop (analysis-only)
uv run python -m kinetic_devops analysis legacy-intake
```

Common environment variables:
- `KINETIC_ACTIVE_ROOT` (default: `D:\Kinetic_SDK`)
- `KINETIC_AI_BASE_URL` (fallback only, e.g. for runners without a keyring; normally the AI server is
  stored in the keyring with `uv run python -m kinetic_devops.auth ai-set`, which also stores its API
  key, API style (`openwebui` or `openai`) and default model. Profiles are named (`ai-set --name fast`,
  `--name deep`), share one stored key per server, and are chosen with `analysis --ai-profile NAME`
  (default: `auth ai-default NAME`); see `auth ai-show` / `auth ai-clear`.
  Order: `--ai-base-url`, then the selected/default profile, then this variable)
- `KINETIC_AI_TIMEOUT` (default: `180`)
- `KINETIC_ANALYSIS_MAX_COMMITS` (default: `20`)
- `KINETIC_ANALYSIS_LEGACY_OUT_DIR` (default: `D:\Kinetic_SDK\temp\legacy_intake_reviews`)

Example:

```powershell
Copy-Item scripts\ai_env.local.ps1.example scripts\ai_env.local.ps1
uv run python -m kinetic_devops analysis diff-review
```

### MetaFX Layer DevOps Flow

```powershell
# Dry-run: parse dump files and resolve layer operations without sending requests
python -m kinetic_devops meta --env <ENV> --user <USER> layers "C:/path/dump (5).json" "C:/path/dump (6).json" --dry-run

# Execute: run import/delete operations and capture status + correlation IDs in a report
python -m kinetic_devops meta --env <ENV> --user <USER> layers "C:/path/dump (5).json" "C:/path/dump (6).json" --report temp/layer_ops_report.json
```

Default behavior for `layers` is deployment mode (`--ops import`):
- Executes delete operations first (when present)
- Then executes import operations

This flow is designed to support layer deployment and rollback as part of the DevOps cycle.

### ExportAllTheThings Full Export Flow

Export everything currently exposed by your ExportAllTheThings library:

```powershell
# Export all discovered Export* functions and write a manifest
python -m kinetic_devops export --env <ENV> --user <USER> --library ExportAllTheThings --out-dir exports/ExportAllTheThings

# List discovered export functions only
python -m kinetic_devops export --env <ENV> --user <USER> --list

# Run one export function
python -m kinetic_devops export --env <ENV> --user <USER> --function ExportAllCustomBAQs

# Run full export across all configured companies (dedup enabled by default)
python -m kinetic_devops export --env <ENV> --user <USER> --all-companies --out-dir exports/ExportAllTheThings
```

The export command:
- Discovers export-capable functions from the EFx library (FunctionID starts with Export)
- Executes one or all exports
- Saves each artifact file
- Writes an export manifest JSON with per-function success/failure details

Mode behavior:
- `--mode auto` (default): uses ExportAllTheThings when present, otherwise native endpoints
- `--mode eatt`: requires EFx export library behavior
- `--mode native`: no ExportAllTheThings dependency
- `--all-companies`: runs exports once per configured company (or use `--companies CO1 CO2`)
- de-duplication is enabled in `--all-companies` mode and records duplicate pointers in the manifest (`--no-dedup` disables this)

Native examples (no ExportAllTheThings required):

```powershell
# Run built-in native export plan for BAQs, directives, UD codes, MetaFX app list, etc.
python -m kinetic_devops export --mode native --env <ENV> --user <USER> --out-dir exports/native

# Pull one endpoint at a time
python -m kinetic_devops export --mode native --env <ENV> --user <USER> --native-endpoint "/api/v2/odata/{company}/Ice.BO.BAQDesignerSvc/GetList" --native-method POST --native-body '{"whereClause":"SystemFlag=false","pageSize":0,"absolutePage":0}' --native-name baq_list_single

# Run native export plan across all configured companies
python -m kinetic_devops export --mode native --env <ENV> --user <USER> --all-companies --out-dir exports/native
```

### Solution Workbench Backup And Recreate

Solution migration is a different workflow from package build/install.

- `BuildSolution` and `InstallSolution` operate on a built package artifact
- `GetByID`, `GetNewExportPackage`, and `Update` operate on the solution definition dataset
- Recreating the solution in the target environment lets you build it there later

Typical operational sequence:
- `create` in Kinetic UI (author/edit your solution)
- `backup` in DevOps
- `build` in DevOps (when you need a package artifact)
- `install` in DevOps (when promoting by package)
- `recreate` in DevOps (when migrating editable solution definition state)

You can use package promotion (`build` + `install`) and definition migration (`recreate`) independently or together.

Examples:

```powershell
# Back up a solution definition and its tracked-item snapshots
python -m kinetic_devops solutions --env <SOURCE_ENV> --user <USER> backup MySolution --out-dir exports/solutions

# Recreate that solution definition in a target environment
python -m kinetic_devops solutions --env <TARGET_ENV> --user <USER> recreate exports/solutions/solution_backup_MySolution_20260331T000000Z.json

# Recreate under a new Solution ID, replacing an existing target solution if needed
python -m kinetic_devops solutions --env <TARGET_ENV> --user <USER> recreate exports/solutions/solution_backup_MySolution_20260331T000000Z.json --target-solution-id MySolution_Copy --overwrite

# Build a CAB package from a solution on the server
python -m kinetic_devops solutions --env <ENV> --user <USER> build MySolution

# Install a CAB package
python -m kinetic_devops solutions --env <ENV> --user <USER> install temp/MySolution.cab

# Install with automatic tenant-layer conflict cleanup and one retry
uv run kinetic-devops solutions --env <ENV> --user <USER> install temp/MySolution.cab --overwrite-duplicate-file --overwrite-duplicate-data --override-directives --auto-clear-layer-conflicts
```

Current recreate behavior is intentionally focused on the Solution Workbench definition:
- solution header and package rows
- solution detail membership rows
- backup capture of per-table dynamic/tracked snapshots for later analysis

`recreate` does not implicitly call `build` or `install`; those are explicit steps.

### EFx Function Library Management

Export/import/list EFx Function Libraries (`Ice.Lib.EfxLibraryDesignerSvc`), and pull/push individual
functions' C# source for local editing:

```powershell
# List visible libraries
python -m kinetic_devops.efx_library --env <ENV> --user <USER> list

# Export a single library, or every visible library (one file per library)
python -m kinetic_devops.efx_library --env <ENV> --user <USER> export <LibraryID> --out backup.efxlib
python -m kinetic_devops.efx_library --env <ENV> --user <USER> export-all --out-dir backups/

# Import a previously exported library
python -m kinetic_devops.efx_library --env <ENV> --user <USER> import backup.efxlib --new-library-id CopyOfLib

# Pull a library's functions as editable .cs files, edit one, push it back
python -m kinetic_devops.efx_library --env <ENV> --user <USER> pull-source <LibraryID> --out-dir projects/libraries
python -m kinetic_devops.efx_library --env <ENV> --user <USER> push-source <LibraryID> <FunctionID> --out-dir projects/libraries

# Actually execute a saved function -- only in the environment named by --allow-env (default: Epicor's Pilot tier), interactive confirmation required, never unattended
python -m kinetic_devops.efx_library --env <env> --user <USER> test-pilot <LibraryID> <FunctionID> --allow-env <env>
```

`pull-source` extracts each function's embedded C# (`CustomCodeAction.Code` fragments inside the
function's directive graph) into one `.cs` file per code node, plus a `_body.xml` sidecar and a
`_meta.json`. `push-source` re-injects edited `.cs` files back into a freshly-fetched copy of the
function (not the local sidecar, to avoid clobbering concurrent changes) via
`ApplyChangesWithDiagnostics`, and raises if the server reports any diagnostics.

`tools/efx_syntax_check` is a Roslyn-based syntax-only validator for extracted `.cs` fragments
(no semantic/type checking -- Epicor's runtime types aren't available outside the server).

Note: pulled function source (actual customer business logic) is intentionally kept out of this repo
-- `projects/` is gitignored. Store pulled libraries in a separate, dedicated location.

See [examples/complete_flow.py](examples/complete_flow.py) for a complete auth → BAQ → results example.

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) — Contributor guide: coverage matrices, service module pattern, dual-remote push convention
- [ARCHITECTURE.md](Documents/ARCHITECTURE.md) — Design principles, workflow examples
- [scripts/README.md](scripts/README.md) — Helper scripts reference
- [tests/README.md](tests/README.md) — Test suite documentation

## Known Limitations

- **Alpha stage** — Breaking changes may occur between versions
- **Redaction edge cases** — Heuristic-based sensitive data masking has known gaps
- **Windows-first** — Tested primarily on Windows; Linux/macOS support improving

## Project Structure

```
kinetic-devops/           # Core library (published)
├── src/kinetic_devops/   # Main package (src layout)
├── scripts/              # Helper scripts
├── tests/                # Test suite
└── projects/             # (Optional) User project submodules
```

Users extend this core by adding private Git submodules for their custom implementations.

## Requirements

- Python 3.10+
- `keyring` — Secure credential storage
- `requests` — HTTP client

## Installation

Using `uv` (recommended):
```bash
uv pip install kinetic-devops
```

Or with pip:
```bash
pip install kinetic-devops
```

From source with `uv`:
```bash
git clone https://github.com/<owner>/kinetic-devops.git
cd kinetic-devops
uv run kinetic-devops
```

## License

See [LICENSE](LICENSE) file.
