# Plan: live security / SoD audit in kinetic-devops

Status: **draft for discussion** (2026-10-26). Replaces the standalone `kinetic-security-audit` script (Excel exports, `ollama`, hard-coded paths) with a module in this package that pulls security data live, read-only, and evaluates it against a reviewable SoD ruleset.

## 1. Goal

For one Kinetic environment, answer: **which users can perform which business functions, through which grants, and which of them hold conflicting functions** (segregation of duties). The answer must be:
- **Deterministic:** conflicts are computed from rules and the security model, never decided by a model.
- **Evidenced:** every finding names the grant path (user → group → security code → menu item / BO method).
- **Repeatable:** snapshots per run; diffs between runs show new and removed conflicts.

## 2. Why rebuild instead of reuse

The old script re-implements what this package already does better: authentication and environments (`auth`, keyring), REST access (`KineticBaseClient.execute_request`, `KineticBOReaderService.get_list`, `KineticBAQService`), service discovery (`scripts/pull_api_store.py`), API keys and access scopes (`access_scope`), AI profiles and review budgets (`analysis`, `auth ai-set`), the sensitive-data gate, uv, and the test/CI setup. **Kept from it as ideas only:** walking the menu hierarchy for context, checkpoint/resume, robust JSON parsing of model answers, the logical-access-matrix concept.

## 3. Inputs

| Input | Source | Notes |
|---|---|---|
| Users, their security groups, flags (disabled, **security manager**), company/site access | `Erp.BO.UserFileSvc` (already used in `auth`) | Security managers bypass most checks: report them separately, not as conflicts |
| Security groups | `Ice.BO.SecGroupSvc` (to verify) | |
| Menu items and hierarchy | `Ice.BO.MenuSvc` (to verify) | Menu id, parent, program/BO, security code |
| Security codes: allow-all flag, entry list (allowed users/groups), no-entry list (denied) | `Ice.BO.SecSvc` / BAQ over `Ice.Sec` (to verify) | The old exports' `Calculated_AllowAll` and `Security_EntryList` columns came from here |
| BO method security | method-security service / BAQ (to verify) | Second grant path besides menus |
| API keys and access scopes | `access_scope.fetch_api_keys`, `export_access_scope` | Integration accounts are users too |
| Function catalogue + SoD rules | `sod-ruleset` (`functions.csv`, `sod_rules.csv`), path or package data | Generic default; organisation-specific copies stay private |
| Mapping: Kinetic menu items / BO methods → functions | `mapping.csv` maintained per organisation | Starts from `kinetic_bo_candidate`; extended with verified menu ids |

**Read path decision (P0):** BO reads (`GetRows` / `GetList`) where they expose the fields, **BAQs** where a join is simpler (e.g. `Ice.Sec` × `Ice.Menu`). BAQ definitions are shipped as text exports in this repo and installed by an admin; the auditor account only needs to run them.

## 4. Design

New package `kinetic_devops.security_audit` and CLI group `kinetic-devops security-audit`:

| Command | Does |
|---|---|
| `pull --env E` | Read-only pull of §3 into a timestamped snapshot directory (JSON, one file per source + `manifest.json` with environment, user, counts, hashes). **Method allowlist:** only `GetRows` / `GetList` / `GetByID` / BAQ `GetList`; anything else is refused in code |
| `verify-mapping` | Checks every mapping entry against the pulled service list (`pull_api_store`) and menu snapshot; reports unknown BOs/menu ids; sets `verified` |
| `resolve` | Effective permissions per user: security manager → all; else deny (no-entry) beats allow; allow-all codes; user and group entries; menu and method paths. Output: `user × function` with evidence paths |
| `sod` | Applies `sod_rules.csv` to the resolved permissions: per user, every rule whose two functions are both held, with risk and both evidence paths. Mitigating controls file (`mitigations.csv`: user, rule, control, owner, expiry) marks accepted conflicts |
| `report` | Markdown + CSV (findings, per-user summary, security managers, unmapped menu items, API keys/scopes) |
| `diff A B` | New / removed conflicts and grants between two snapshots |
| `suggest-mapping` (optional, AI) | For unmapped menu items, asks the local model (AI profile + budget, `openai-chat`) to propose a function id using the menu path as context; writes **suggestions only** for a human to accept into `mapping.csv` |

Data model: plain dataclasses + JSON; no database. Reuse `analysis` helpers for AI calls (chunking, budgets, `VERDICT`-style strict output → here a strict JSON line).

## 5. Safety and data handling

- **Read-only by construction** (method allowlist), auditor account with minimum rights.
- **Outputs are company data:** written outside the repo by default (`<active root>/temp/security_audit/…`), `.gitignore`d, never committed; `--pseudonymize` replaces user ids with stable hashes for sharing.
- The sensitive-data gate runs over the repo as today; snapshots are excluded from any upload.
- **AI is advisory:** it never decides conflicts or grants; it only suggests mappings, and only via the local AI profile.
- Tests and CI use **synthetic fixtures only** (generic users/groups/menus built from the generic `sod-ruleset`); no live calls in CI.

## 6. Phases

| Phase | Deliverable | Check |
|---|---|---|
| **P0 Spike** | On a test environment: confirm services/BAQs and fields for §3, rights the auditor account needs, Kinetic version (cloud/on-prem) differences | Short findings note in this file; §3 "to verify" resolved |
| **P1 Pull** | `pull` with method allowlist, snapshot + manifest, checkpoint/resume per source | Synthetic-fixture tests; live pull on test env completes read-only |
| **P2 Resolve** | Effective-permission engine with evidence paths | Unit tests for precedence (security manager, deny > allow, allow-all, group vs user, menu vs method) |
| **P3 SoD + report** | `sod`, `report`, mitigations | Fixture with known conflicts → exact expected findings; generic ruleset passes |
| **P4 Mapping** | `verify-mapping`; organisation mapping starts from `kinetic_bo_candidate` | Every generic function mapped and verified on the test env |
| **P5 Diff / schedule** | `diff`; documented quarterly run | Two snapshots with a planted change → diff shows exactly it |
| **P6 AI suggestions** | `suggest-mapping` | Suggestions written, never applied automatically; accepted ones tracked in `mapping.csv` history |

Per the Definition of Done: CLI help and README usage, CHANGELOG, regression tests, coverage matrix (`tests/test_module_coverage_matrix.py`, `tests/cli_matrix.py`) updated with the new module.

## 7. Open questions

1. Which security layers are in scope first: menu security only, or also BO method, field security, company/site access, CRM/territory security?
2. Auditor account: dedicated, read-only, and who approves its rights?
3. Where findings go (who receives the report), retention of snapshots, pseudonymization by default?
4. Organisation-specific rules and mappings: kept in a private repo at work, separate from the generic `sod-ruleset`.
5. BAQ installation: shipped as exports here and installed per environment by an admin, or created by `kinetic-devops baq` tooling?
