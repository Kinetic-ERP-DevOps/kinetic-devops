# Scripts & Developer Tooling

## Environment Setup (Kinetic Config)

Run `uv sync` once: it installs `kinetic_devops` (from `src/`) into `.venv`, so helper scripts import it without any `PYTHONPATH`. Then initialize the Kinetic configuration variables:

### Windows (Command Prompt / batch):

```batch
scripts\env_init.bat [env-name]
```

This activates the uv venv (`.venv`, if present) and sets:
- `KIN_COMPANY`, `KIN_ENV_NAME` (script context vars)
- `KINETIC_TAXCONFIG_DB` (SQLite config location)

The script generates and executes a randomized temp `.bat` file under your OS temp directory, then securely erases it.

### Windows (PowerShell):

```powershell
. .\scripts\env_init.ps1 [env-name]
```

Same behavior as `.bat`, but for PowerShell. Dot-source the script to inherit environment variables in your session.

To keep AI endpoint configuration out of source control, create `scripts/ai_env.local.ps1` from the example file and let `env_init.ps1` source it automatically.

### Unix-like (bash/sh):

```bash
. ./scripts/env_init.sh [env-name]
```

Activates a Python venv and sets environment variables.

---

## Running Tests

After environment setup, run the canonical test runner:

```powershell
uv run python -m tests.test_runner
# or
uv run python tests/test_runner.py
```

- The test runner validates the environment, discovers tests under `tests/`, and writes results to `tests/test_results.log`.
- CI runs the same tests via `uv run python -m pytest` (Linux on GitHub and Forgejo, plus `windows-latest` on GitHub), which also records per-test timings (see "Comparing Environments").

---

## Git Hooks

Install the hooks once per clone (PowerShell):

```powershell
.\scripts\install-hook.ps1
```

- **pre-commit** (always on): a blocking sensitive-data scan of the staged content, then the test
  suite with native checks deferred (see below). It runs through `uv run`.
- **pre-push** (opt-in): tests each pushed commit on a clean Windows in Windows Sandbox
  (`scripts/sandbox_test.py`, several minutes). Enable with `git config kinetic.prepushSandbox true`.
  Without Windows Sandbox (not every developer has it) the check is skipped with a warning and the
  push continues. `KINETIC_PREPUSH_SANDBOX=0` skips it for one push even when enabled (`=1` runs it
  once without enabling it). Commits that already passed are skipped, so you can run
  `uv run python scripts/sandbox_test.py` before pushing.

### Native checks and enterprise endpoint security

Managed Windows machines may block or kill **newly built or edited unsigned code**: the console-script
launchers uv writes (`.venv\Scripts\kinetic-devops.exe` and friends), native extension libraries
of newly installed packages (e.g. `cryptography`'s, used by the encrypted vault) and new or recently
edited `.ps1` files. Symptoms: "Access denied" when running a script, `DLL load failed ... Access is
denied` on import, a launcher killed mid-run, or a file briefly locked right after it is written. `uv run` itself is fine and remains the way to run everything.

So the checks that execute that native code are **deferred** on a development machine, never dropped:

- Local test runs and the pre-commit hook skip them (the same commands are still covered via
  `python -m`), so development is never blocked.
- CI always runs them (`CI=true`), including GitHub's `windows-latest` job.
- Windows Sandbox runs them on demand: `uv run python scripts/sandbox_test.py` (full suite plus smoke
  checks), `--native-only` (just the deferred checks), `--tests NAME ...` (a subset), `--smoke-only`.
- Run them on this machine intentionally, e.g. while getting the files approved by your security
  team: `uv run python -m tests.test_runner --native` and `.\scripts\windows_smoke.ps1`
  (or `KINETIC_NATIVE_CHECKS=1` for any run).

Endpoint security also costs time. On one managed machine (freshly rebooted, nothing else running)
the same suite, same commit and hardware, took 67 s natively, 21 s in the kept Windows Sandbox and
9 s in WSL; over the tests that ran everywhere, native was 4x slower than the sandbox, most of all in
tests that start Python processes (5-7x) and git/file-heavy tests (about 3x). Machine state matters
too: before a reboot every environment there ran 2-4x slower. On such machines, prefer the kept
sandbox for full local runs: `uv run python scripts/sandbox_test.py --working-tree`, and compare with
`scripts/run_timings.py`.

If running `install-hook.ps1` is blocked, the hooks can still be installed: they are the two `sh`
scripts embedded in it (`$hookContent` -> `.git/hooks/pre-commit`, `$prePushContent` ->
`.git/hooks/pre-push`).

## Windows Checks

- `scripts/windows_smoke.ps1`: the Windows-only checks (console-script launchers, `env_init.ps1` and
  `env_init.bat`, `install-hook.ps1`). Run it from the repo root of a throwaway checkout after
  `uv sync`. GitHub's `windows-latest` CI job runs it on every pull request.
- `scripts/sandbox_test.py`: runs the test suite and the smoke checks for a commit on Windows in
  Windows Sandbox (no endpoint-security agent). With the Windows Sandbox CLI (`wsb`, Windows 11 24H2+)
  one sandbox is kept running and reused: each run checks what is running first, reuses its sandbox,
  adopts a running one it doesn't know (stale id, earlier session, opened by hand -- the job folder is
  shared into it with `wsb share`), or starts one. Each job gets a fresh checkout and `.venv`; uv and
  its cache stay warm. `--fresh` restarts it clean, `--stop` stops it, `--one-shot` uses a self-closing
  sandbox (the fallback without `wsb`). Exit 0 pass, 1 fail, 2 sandbox unavailable.
  Git in the kept sandbox is MinGit on the sandbox's own disk. This machine keeps the zip in a
  version-checked cache (`%LOCALAPPDATA%\kinetic-devops\sandbox\cache`): it checks the latest
  git-for-windows release, downloads only when the cache is missing, outdated or corrupt, and checks
  the SHA-256 against the release's published digest; offline it uses the cached copy. The sandbox
  reinstalls when its version differs. `--refresh-mingit` ignores the cache (download and reinstall);
  `--no-mingit` uses this machine's Git instead.

## Comparing Environments (timings)

Informative only -- nothing here fails a build. Each environment records a run as JUnit XML from
`python -m pytest --junitxml` (per-test time, outcome and skip reason) plus a small metadata file
(label, commit, Python, OS, CPUs, end-to-end time, extra checks included).

- `uv run python scripts/run_timings.py [--working-tree] [--envs native,sandbox,wsl,alpine] [--repeat N]`
  runs the suite on this machine, in the kept Windows Sandbox, in WSL (default: the newest
  `Ubuntu-NN.NN` distro with uv; `--wsl-distro` to choose) and in a throwaway `python:3.14-alpine`
  container via Podman in a WSL distro that has it (musl coverage; `--podman-distro`), each skipped
  with the reason if unavailable, and writes the records to `temp/timings/<commit>/`. With
  `--repeat N` each environment runs N times and the report compares medians (with the min-max
  range, and tests that passed in some repeats and failed in others marked flaky). Each record notes
  how busy this machine's CPU was when the run started; runs that started 50% busy or more are
  flagged as unreliable.
- CI records every run too: download the `timings-*` artifacts of a CI run (GitHub Linux/Windows,
  Forgejo Linux) into the same folder to compare with the local environments.
- `uv run python scripts/compare_timings.py report temp/timings/<commit>/ [--baseline windows-native]`
  prints: totals per environment (suite time and end-to-end time, with each run's scope -- tests run,
  skipped, failed, extra checks -- and where scopes differ, what differs, so apples-to-oranges totals
  are labelled as such); tests not run in every environment, with the skip reasons; failures that
  happen in only some environments; and per-module/per-test times over the tests that passed
  everywhere (the like-for-like comparison).

Always invoke pytest as `python -m pytest`: endpoint security may block the generated `pytest.exe`
launcher.

## Branch Protection Automation (GitHub + Forgejo)

Use the config-driven automation script to apply branch protection across multiple hosts.

Config template:

```powershell
Copy-Item scripts\branch_protection.targets.example.json scripts\branch_protection.targets.json
```

Set your tokens in the current shell:

```powershell
$env:GITHUB_TOKEN = "<github-token>"
$env:FORGEJO_TOKEN = "<forgejo-token>"
```

Or store tokens encrypted at rest in OS keyring (recommended):

```powershell
python -c "import keyring; keyring.set_password('kinetic-devops-tokens','github.com/my-org','<github-token>')"
python -c "import keyring; keyring.set_password('kinetic-devops-tokens','forgejo.local/my-org','<forgejo-token>')"
```

Token resolution order:
- Environment variable first (for CI/CD and temporary overrides)
- Keyring fallback (`kinetic-devops-tokens` service by default)
- Keyring account match order: explicit account, `host/owner`, `host`, then legacy provider keys

Unified smoke wrapper (auto-detect provider from git remote):

```powershell
uv run python scripts/repo_maker.py
uv run python scripts/repo_maker.py --apply
```

Modular package commands (same behavior, namespaced under RepoMaker):

```powershell
uv run python -m kinetic_devops.repomaker reposmith --apply
uv run python -m kinetic_devops.repomaker apply --config scripts/branch_protection.targets.json --apply
```

Installed console scripts (after package install):

```powershell
repomaker reposmith --apply
repomaker apply --config scripts/branch_protection.targets.json --apply
reposmith --apply
```

Dry-run preview (default):

```powershell
uv run python scripts/apply_branch_protection.py --config scripts/branch_protection.targets.json
```

Apply changes:

```powershell
uv run python scripts/apply_branch_protection.py --config scripts/branch_protection.targets.json --apply
```

The script supports:
- GitHub branch protection via `https://api.github.com`
- Forgejo branch protection via a configured `forgejo_api_base` (for example `https://forgejo.example.com/api/v1`)
- Multiple repositories/hosts in one run

Auto-detection behavior:
- If `owner`/`repo` are omitted for a target, the script uses `git remote.origin.url` from the current repo.
- If `provider` is omitted, the script infers it from remote host (`github.com` -> `github`; otherwise `forgejo`).
- For inferred Forgejo targets, `forgejo_api_base` defaults to `<remote-host>/api/v1`.

CI workflow mirrors:
- GitHub Actions: `.github/workflows/ci-tests.yml`
- Forgejo Actions: `.forgejo/workflows/ci-tests.yml`

Use branch protection rules on both platforms to require only these blocking checks before merge:
- `Python Test Gate (required, py3.10)`
- `Python Test Gate (required, py3.12)`

Keep advisory checks non-required so merges can proceed while still flagging version compatibility issues.

### Full-Stack Forgejo Smoke (API)

You can validate the full lifecycle against a fresh Forgejo repository using API calls only.

Dry-run preview:

```powershell
$env:FORGEJO_URL = "https://forgejo.local"
$env:FORGEJO_OWNER = "my-org"
python scripts/forgejo_fullstack_smoke.py
```

Apply mode (creates repo, applies branch protection, verifies it, then deletes repo by default):

```powershell
$env:FORGEJO_TOKEN = "<forgejo-token>"
python scripts/forgejo_fullstack_smoke.py --apply
```

Keyring-backed token path (no token env var required):

```powershell
python scripts/forgejo_fullstack_smoke.py --apply --token-service kinetic-devops-tokens --token-account forgejo.local/my-org
```

Keep the temporary repository for manual inspection:

```powershell
python scripts/forgejo_fullstack_smoke.py --apply --keep-repo
```

Common options:
- `--forgejo-url` (or `FORGEJO_URL`) - Forgejo base URL
- `--owner` (or `FORGEJO_OWNER`) - owner/org for repository creation
- `--owner-type org|user` - creation endpoint type
- `--repo` - explicit repository name (otherwise generated)
- `--required-check` - required status check context (default: `Python Test Gate`)

### Full-Stack GitHub Smoke (API)

You can validate the same lifecycle against a fresh GitHub repository using API calls only.

Dry-run preview:

```powershell
$env:GITHUB_OWNER = "my-org"
python scripts/github_fullstack_smoke.py
```

Apply mode (creates repo, applies branch protection, verifies it, then deletes repo by default):

```powershell
$env:GITHUB_TOKEN = "<github-token>"
python scripts/github_fullstack_smoke.py --apply
```

Keyring-backed token path (no token env var required):

```powershell
python scripts/github_fullstack_smoke.py --apply --token-service kinetic-devops-tokens --token-account github.com/my-org
```

Keep the temporary repository for manual inspection:

```powershell
python scripts/github_fullstack_smoke.py --apply --keep-repo
```

Common options:
- `--owner` (or `GITHUB_OWNER`) - owner/org for repository creation
- `--owner-type org|user` - creation endpoint type
- `--repo` - explicit repository name (otherwise generated)
- `--required-check` - required status check context (default: `Python Test Gate`)

---

## Helper Scripts

Once environment is initialized, helper scripts can be invoked:

```powershell
# Validate environment (check Python version, imports, requests module)
python scripts/validate.py

# Pull tax configs for all companies and save to Data/tax_configs/
python scripts/pull_tax_configs.py [--env ENV] [--out-dir PATH] [--dry-run]

# Pull all discovered Kinetic Swagger/OData/method specs into projects/Kinetic-API-Store/
python scripts/pull_api_store.py [--env ENV] [--user USER] [--company COMPANY] [--out-dir PATH] [--format json|yaml|both] [--surface odata|methods|both]
python scripts/pull_api_store.py --surface methods --service <SERVICE>

# Run core layer import/delete operations from BO-call dump JSON exports
python -m kinetic_devops meta --env <ENV> --user <USER> layers "C:/Users/<you>/Downloads/dump (5).json" "C:/Users/<you>/Downloads/dump (6).json"
python -m kinetic_devops meta --env <ENV> --user <USER> layers dump5.json dump6.json --ops import
python -m kinetic_devops meta --env <ENV> --user <USER> layers dump5.json dump6.json --dry-run

# Trim trace files and generate a delta sidecar for review
uv run python -m kinetic_devops.trim_trace_paths trace.json --drop "*.request.headers.Authorization" --delta-mode summary --delta-format jsonc
uv run python -m kinetic_devops.trim_trace_paths trace.json --drop "*.request.headers.Authorization" --delta-mode annotated --delta-format both --no-delta-redact-values

# Migrate API key access scope from one instance to another (direct remap)
python -m kinetic_devops scope --source-env SourceEnv --target-env TargetEnv --from-scope LEGACY_SCOPE --to-scope NEW_SCOPE

# Detach scope first, run fresh export/import, then rebind and verify (only needed when the scope is already in use)
python -m kinetic_devops scope --source-env SourceEnv --target-env ProductionEnv --from-scope PREIMPORT_SCOPE --to-scope PROD_SCOPE --strategy detach-rebind --pause-for-import

# First-class validation: compare target scope definition against production/reference env
python -m kinetic_devops scope validate --scope-id MyScope --reference-env ReferenceEnv --target-env TargetEnv --report exports/System-Apps/AccessScopeMigr/MyScope_scope_functional_compare.json

# Validate target against a saved production artifact and keep exit success on known drift
python -m kinetic_devops scope validate --scope-id MyScope --reference-artifact exports/System-Apps/AccessScopeMigr/MyScope_reference_snapshot.json --target-env TargetEnv --allow-drift
```

### pull_tax_configs.py options:

- `--env ENV` — Only pull for a specific environment nickname (optional)
- `--out-dir PATH` — Output directory (default: `Data/tax_configs/`)
- `--dry-run` — Show summary without writing files

### pull_api_store.py options:

- `--env ENV` — Target environment nickname (optional)
- `--user USER` — Optional user session override
- `--company COMPANY` — Optional company context override for authentication
- `--out-dir PATH` — Output root directory (default: `projects/Kinetic-API-Store`)
- `--format json|yaml|both` — Which spec formats to download (default: `json`)
- `--surface odata|methods|both` — Pull only OData specs, only method specs, or both (default: `both`)
- `--service-type BO|Proc|Lib|Rpt|UD` — Filter to a service category
- `--service NAME` — Pull only an exact service name; may be repeated
- `--env-subdir` — Store files under an environment subdirectory instead of directly under the output root
- `--missing-only` — Skip files that already exist instead of checking for updates
- `--overwrite` — Re-download files even when they already exist
- `--connection-version VERSION` — Explicit connection version (for example `2025.2.14`) used to compute major version (`2025.2`)
- `--force-scan` — Bypass major-version gate and pull anyway
- `--timeout SECONDS` — HTTP timeout per request (default: `60`)
- `--show-discovery` — Print discovery endpoint statuses while scanning

`pull_api_store.py` discovers specs from both:
- old Rest Help pattern: `https://<host>/<instance>/api/help/v2/index.html`
- new Rest Help service listings: `https://<host>/<instance>/api/helppage/services?serviceType=BO|Proc|Lib|Rpt|UD`
- swagger endpoints: `https://<host>/<instance>/api/swagger/v2/odata/...` and `https://<host>/<instance>/api/swagger/v2/methods/...`

It stores files under surface folders to avoid collisions:
- `projects/Kinetic-API-Store/odata/`
- `projects/Kinetic-API-Store/methods/`

It writes:
- `manifest.json` with discovery status, pull filters, a best-effort Kinetic server version probe, and **redacted** request headers
- `_pulls/pull_<timestamp>.json` as a point-in-time pull record for version control history
- `connection-history.yaml` as a git-friendly text history keyed by connection version and major version

Privacy behavior for metadata files:
- raw URLs are not persisted in `manifest.json` or `connection-history.yaml`
- raw service IDs and tenant IDs are not persisted in metadata summaries
- connection identity is stored as a fingerprint, not as plain URL/env/user/company

The default pull mode is git-friendly sync:
- git is the history of record for spec changes
- existing files are fetched and only rewritten when content changes
- unchanged files are reported without creating noisy file updates
- if the major version (for example `2025.2`) is already recorded in `connection-history.yaml` for the environment, the pull is skipped by default
- use `--missing-only` when you want resume-only behavior and do not want to check existing files
- use `--force-scan` to run a pull even when the major-version gate would skip it
- if you cancel a run, the script writes a partial manifest instead of losing progress
- use `--overwrite` only when you want to force rewrite files already present

### `meta layers` options:

- `--ops import|delete|both` — Filter which layer operation types are executed (default: `import`)
- `--env ENV` — Target environment nickname
- `--user USER_ID` — Optional user session override
- `--company COMPANY` — Optional company override
- `--tenant-company T_ID` — Target tenant company ID used in company-scoped layer operations while keeping working company context
- `--plant PLANT` — Optional plant value used in `callSettings`
- `--dry-run` — Parse and resolve calls without sending HTTP requests
- `--report PATH` — Output JSON report path (default: `temp/layer_ops_report_<timestamp>.json`)

`json_helper.py build --deploy` operation control:
- default: delete then import (full deploy)
- `--delete-only` — perform delete only (useful for tenant-company cleanup)
- `--import-only` — perform import only
- `--skip-delete` — legacy alias behavior for import without delete

Example (delete-only against tenant company):
`python scripts/json_helper.py build <layer.jsonc> --deploy --delete-only --tenant-company <TENANT_ID> --env <ENV> --user <USER>`

Sensitive-value handling (preferred):
- use keyring-backed environment selection whenever possible
- use environment variables instead of CLI literals for sensitive runtime context:
	`KIN_ENV_NAME`, `KIN_USER` (or `KIN_USER_ID`), `KIN_COMPANY`, `KIN_TENANT_COMPANY`

The `meta layers` command extracts `ImportLayers` and `BulkDeleteLayers` calls from provided dump files,
executes them against the active environment, and captures status, correlation IDs, and
response bodies/errors in the report.

Default `--ops import` behavior is deployment mode: delete first, then import.

---

## Notes

- `PYTHONPATH` is set during environment initialization, enabling imports from the repository root.
- Temporary env files are randomized under the OS temp directory and securely wiped after use.
- The `pull_tax_configs.py` script uses stored Kinetic configuration from the keyring to authenticate.
