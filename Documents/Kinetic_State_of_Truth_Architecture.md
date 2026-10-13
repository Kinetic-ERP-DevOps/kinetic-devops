# 1. Proposed Architecture

## Problem framing

*   Environment state in Kinetic is **non‑deterministic** unless it is actively reconciled.
*   Any deploy‑only model will fail over time because environments inevitably mutate outside pipelines (UI edits, emergency fixes, cloud‑side actions).

## Core principle

*   Explicitly separate **intent**, **expectation**, and **reality**.
*   Never allow a single artifact to claim to be “the truth.”

## Structural solution

A single repository with clearly separated responsibilities:

*   **`/projects` folder** — authoritative deployable units
*   **Master fork of `/projects`** — environment‑neutral canonical source
*   **Environment branches** — declared deployed state pointers
*   **Environment manifests** — expected state contracts
*   **Backup forks** — observed reality snapshots

***

## The Project Folder Master Fork

The `/projects` folder is not a convenience directory; it is a **governance boundary**.

It contains:

*   Script bundles
*   Report packages
*   Function artifacts
*   Any deployable Kinetic unit that can be meaningfully versioned

Critically, `/projects` is:

*   **Environment‑agnostic**
*   **Credential‑free**
*   **Free of environment assumptions**
*   **Ignored by kinetic\_devops**, so local experimentation cannot mutate deployable state

This allows `/projects` to function as a **pure artifact registry**, not a working directory.

The **master fork (or protected branch)** of `/projects` is the *only* source from which **Training and Production promotions** are allowed to occur. No environment may receive anything that does not already exist here.

It defines:

*   What *can* exist anywhere
*   What *may* be promoted
*   What is considered reviewable work

Developers do not deploy “their branch” into higher environments. Pipelines deploy **approved artifacts**. Promotion is a controlled reference move, not a file copy.

***

## Dev as an Integration Environment (Feature Branch CI/CD)

Development environments serve a different purpose than Training or Production: they are **integration platforms**, not promotion targets.

As such:

*   **Dev environments may accept deployments from short‑lived feature branches**, constrained to `/projects`
*   **Training and Production environments may not**

This enables full CI/CD against a real Kinetic target *before* changes are merged into `main`, while keeping `main` continuously releasable.

Feature branch deployments are explicitly **non‑authoritative**:

*   They do not advance promotion pointers
*   They do not redefine expected state
*   They exist to validate changes prior to merge

***

## Environment‑Driven Deployment Policy

Deployment eligibility is **environment‑owned**, not pipeline‑hardcoded.

Each environment declares its own deployment policy via secure metadata (e.g., Keyring), such as:

*   `IsProduction`
*   `AllowedDeploymentSources` (FeatureBranch, Main, ReleaseTag)

Pipelines evaluate this contract rather than embedding branch logic. This allows:

*   Dev to accept feature branch deployments
*   Training to behave like Production when desired
*   Production to require stricter sources (e.g., signed release tags)

The environment declares *what it will accept*; the pipeline merely enforces it.

***

## Lifecycle

*   Dev work → feature branch under `/projects`
*   CI → build/test on PR
*   Deploy‑to‑Dev → allowed from feature branches (integration testing)
*   Merge → PR into `main` after Dev validation
*   Promote → pipeline deploys from protected `/projects` fork
*   Validate → environment checks
*   Record → environment branch + manifest update
*   Observe → explicit pull/export from environment
*   Reconcile → diff against master fork and manifests

***

## Invariant

*   Humans propose.
*   Pipelines promote.
*   Environments are **observed**, not trusted.

***

## Intent, Expectation, Reality

This model deliberately distinguishes three independent states:

*   **Intent** — what we believe should exist, expressed through Git history and review
*   **Expectation** — what an environment contractually claims to contain, expressed through manifests
*   **Reality** — what is actually present, obtained only through observation and export

None of these is allowed to overwrite the others silently.

***

## Environment Branches as Pointers, Not Workspaces

Branches such as `env/dev`, `env/Training`, and `env/prod` do not contain evolving code. They contain **references**.

They are advanced only by automation, and only after:

1.  Deployment succeeds
2.  Validation passes
3.  The environment is confirmed to match the deployed artifact

They answer a single question:

> “What version did we last declare as deployed here?”

They are historical markers, not collaboration surfaces.

***

## Environment Manifests as Contracts

Manifests sit orthogonally to branches.

They describe:

*   Expected artifacts
*   Expected versions or hashes
*   Optional fingerprints of deployed state

They are designed to be violated.

When reality diverges from a manifest, that divergence is surfaced rather than hidden. Drift becomes observable and discussable instead of anecdotal.

***

## Pull, Snapshot, Reconcile (The Non‑Negotiable Half)

The model explicitly supports **pulling from environments**.

A pull operation:

*   Exports the live environment state
*   Stores it in a **backup fork or protected snapshot branch**
*   Computes diffs against:
    *   The environment branch (declared intent)
    *   The environment manifest (expected state)
    *   The master `/projects` fork (approved artifacts)

Nothing is overwritten. Nothing is “fixed” automatically.

Unexpected changes become **evidence**, not embarrassment.

***

## Why the Backup Fork Exists

The backup fork is not a rollback mechanism.  
It is not a workflow tool.  
It is **forensics**.

It answers:

*   “When did this change first appear?”
*   “Was this ever reviewed?”
*   “Did this exist before promotion?”

In regulated or audit‑sensitive environments, this alone justifies the model.

***

## Resulting Operating Model

*   Developers propose changes to `/projects`
*   Dev environments support CI/CD from feature branches
*   Pipelines are the only entities allowed to promote
*   Training/Production deploy only from approved sources
*   Environments are continuously observed
*   Drift is reconciled explicitly
*   State is **derived**, not assumed

This does not eliminate complexity.  
It **names it**, constrains it, and makes it survivable.
