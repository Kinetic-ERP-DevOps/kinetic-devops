# Contributing to Kinetic DevOps

## Quick Start

```powershell
.\scripts\env_init.ps1
uv sync
uv run python -m tests.test_runner
```

See [README.md](README.md) for the full quick-start and [scripts/README.md](scripts/README.md)
for environment setup and helper script details.

## Before You Open a PR

1. Run the full test suite: `uv run python -m pytest -q` (or `uv run python -m tests.test_runner`).
2. Run the sensitive-data gate: `python scripts/hooks/pre-commit --remote-scan-only`.
3. If you added or removed a module/script/CLI entrypoint, update the coverage matrices (see below) --
   their tests fail on any mismatch, by design.
4. Issues and planned work are tracked on the Forgejo project/issue tracker for this repo; file bugs
   and feature requests there.

## Module/Script/CLI Coverage Matrices (manual registration required)

Three test files enforce an **exact-match** registry between what's declared and what actually exists
on disk. They do not auto-discover new files -- adding a module without updating the matrix is a
guaranteed test failure, by design, so nothing silently ships without at least a smoke test:

| If you add/remove... | Update this file | Enforced by |
|---|---|---|
| a `kinetic_devops/*.py` module | `PACKAGE_MODULE_MATRIX` in `tests/test_module_coverage_matrix.py` | `test_package_matrix_matches_repo_modules` |
| a `scripts/*.py` script | `SCRIPT_MODULE_MATRIX` in `tests/test_module_coverage_matrix.py` | `test_script_matrix_matches_repo_modules` |
| a `kinetic_devops/*.py` module with a `if __name__ == "__main__":` guard (i.e. it's directly CLI-runnable) | `CLI_MODULES` in `tests/cli_matrix.py` | `test_cli_matrix_consistency.py` |
| a tool exposed through the `kinetic_devops.__main__` router | the matching entry's `"router"` key in `tests/cli_matrix.py`, and `kinetic_devops/__main__.py`'s `TOOLS` dict | `test_router_tool_mapping_matches_router_module` |

If you forget, the failure message tells you exactly which registry to update -- but doing it up front
saves a round trip.

## Service Module Pattern

Every current service module follows the same shape: subclass `KineticBaseClient` directly, rather
than wrapping a separate standalone class. See `kinetic_devops/efx_library.py` or
`kinetic_devops/boreader.py` for the current reference pattern:

```python
from kinetic_devops.base_client import KineticBaseClient

class KineticMyThingService(KineticBaseClient):
    def _call(self, method_name, payload=None, company="", http_method="POST"):
        url = f"{self.config['url'].rstrip('/')}/api/v2/odata/{company or self.config['company']}/Ice.Lib.MyThingSvc/{method_name}"
        return self.execute_request(http_method, url, payload=payload, company=company)

    def get_things(self, company: str = ""):
        return self._call("GetThings", company=company)
```

- `self.config` (url, token, api_key, company, nickname) and `self.execute_request()` come from
  `KineticBaseClient` -- don't reimplement auth/header logic in a service module.
- One file per service, one class per file, one method per endpoint.
- Accept/return `dict`/`list`, not rigid parameter objects.
- `Documents/ARCHITECTURE.md` has more background, but defer to the actual code in
  `kinetic_devops/*.py` over that document's diagrams/examples if they ever disagree --
  architecture docs drift faster than tests do.

## Verify Before You Document or Ship

Epicor's REST API surface (`Ice.BO.*`, `Ice.Lib.*` namespaces, payload shapes) is inconsistently
documented upstream and sometimes wrong. Before adding a new service method or asserting that a
namespace/payload/endpoint is right (or wrong), test it live against an authorized environment and
base the implementation on the observed request/response, not on inference from docs or from reading
someone else's code. `scripts/pull_api_store.py` can pull the live Swagger/method spec for a service
(`--surface methods --service <ServiceName>`) if you need to confirm an endpoint's real shape --
reuses the same session auth as everything else, no separate credential needed.

## Dual-Remote Convention (Forgejo + GitHub)

This repo pushes to two remotes that are expected to share history:

- `origin` -- Forgejo (internal)
- `public` -- GitHub (public)

Before pushing new commits to `public`, run a sensitive-data audit on what's being pushed
(`python scripts/hooks/pre-commit --remote-scan-only`, or the full pre-commit hook) and confirm it's
clean. Pushing to `origin` alone doesn't require this extra step. A branch-pointer push that carries
zero new commits (e.g. realigning a ref to an already-pushed tip) doesn't need a fresh audit either.

When rebasing a branch that an open PR depends on, prefer a new branch + fresh PR over force-pushing
over the old one where practical -- GitHub's squash-merge breaks git-level ancestry between a source
branch and its squashed commit on `main`, so a branch that still thinks it's based on the pre-squash
history can show spurious merge conflicts or fail to trigger `pull_request`-triggered CI.

## Pre-commit Hook

```powershell
Copy-Item scripts\hooks\pre-commit .git\hooks\pre-commit
# or
git config core.hooksPath scripts/hooks
```

Runs the sensitive-data gate and the test suite before each commit. See
[tests/README.md](tests/README.md) for more on the test runner.

## Commit Messages

Follow the existing `type(scope): summary` convention visible in `git log` (`feat`, `fix`, `chore`,
`ci`, `docs`, etc.). Keep the summary line under ~70 characters; put the "why" in the body when it's
not obvious from the diff.
