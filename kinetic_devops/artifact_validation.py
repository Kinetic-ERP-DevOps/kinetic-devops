"""Shared artifact normalization and comparison helpers.

This module provides the reusable delta/validation core that domain-specific
commands can build on for consistent CI/CD artifact comparisons.
"""

import json
import os
import zipfile
from typing import Any, Dict, Iterable, List, Mapping, Sequence


DEFAULT_IGNORE_FIELDS = {
    "Company",
    "BitFlag",
    "SysRevID",
    "SysRowID",
    "RowMod",
    "CreatedBy",
    "CreatedOn",
    "ChangedBy",
    "ChangedOn",
    "LastUpdated",
    "LastUpdatedBy",
}

DEFAULT_ARTIFACT_METADATA_FIELDS = (
    "scope",
    "env",
    "company",
    "core_error",
    "entity_error",
    "bom_error",
    "core_raw_count",
    "entity_raw_count",
    "bom_raw_count",
)


def normalize_artifact_rows(rows: Iterable[Mapping[str, Any]], ignore_fields: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Return stable row dictionaries for artifact comparison."""

    ignored = {str(field) for field in ignore_fields}
    normalized: List[Dict[str, Any]] = []

    for row in rows:
        clean: Dict[str, Any] = {}
        for key, value in row.items():
            if str(key).startswith("@") or key in ignored:
                continue
            clean[key] = value
        normalized.append(clean)

    normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return normalized


def compare_artifact_sections(
    reference: Mapping[str, Any],
    target: Mapping[str, Any],
    section_names: Sequence[str],
) -> Dict[str, Any]:
    """Compare matching sections from two artifact payloads."""

    comparison: Dict[str, Any] = {}
    all_identical = True

    for section_name in section_names:
        reference_rows = reference.get(section_name, [])
        target_rows = target.get(section_name, [])

        reference_keys = [json.dumps(row, sort_keys=True, default=str) for row in reference_rows]
        target_keys = [json.dumps(row, sort_keys=True, default=str) for row in target_rows]

        reference_only = sorted(
            key
            for key in set(reference_keys)
            for _ in range(max(0, reference_keys.count(key) - target_keys.count(key)))
        )
        target_only = sorted(
            key
            for key in set(target_keys)
            for _ in range(max(0, target_keys.count(key) - reference_keys.count(key)))
        )
        identical = sorted(reference_keys) == sorted(target_keys)

        comparison[f"{section_name}_identical"] = identical
        comparison[f"{section_name}_reference_only_count"] = len(reference_only)
        comparison[f"{section_name}_target_only_count"] = len(target_only)
        comparison[f"{section_name}_reference_only"] = [json.loads(item) for item in reference_only]
        comparison[f"{section_name}_target_only"] = [json.loads(item) for item in target_only]
        all_identical = all_identical and identical

    comparison["functionally_identical"] = all_identical
    return comparison


def extract_artifact_payload(
    payload: Mapping[str, Any],
    section_names: Sequence[str],
    nested_keys: Sequence[str] = ("reference", "pilot", "source"),
    metadata_fields: Sequence[str] = DEFAULT_ARTIFACT_METADATA_FIELDS,
) -> Dict[str, Any]:
    """Extract a canonical artifact payload from a raw artifact document."""

    source: Mapping[str, Any] | None = None
    if all(key in payload for key in section_names):
        source = payload

    if source is None:
        for nested_key in nested_keys:
            nested = payload.get(nested_key)
            if isinstance(nested, Mapping) and all(key in nested for key in section_names):
                source = nested
                break

    if source is None:
        raise ValueError(f"Artifact JSON does not include {', '.join(section_names)} sections.")

    extracted: Dict[str, Any] = {section_name: source.get(section_name, []) for section_name in section_names}
    for field_name in metadata_fields:
        if field_name in source:
            extracted[field_name] = source.get(field_name)
        elif field_name in payload:
            extracted[field_name] = payload.get(field_name)
        else:
            extracted[field_name] = "" if field_name.endswith("_error") or field_name in {"scope", "env", "company"} else 0
    return extracted


def build_scope_artifact_from_tableset(
    tableset: Mapping[str, Any],
    ignore_fields: Iterable[str] = DEFAULT_IGNORE_FIELDS,
    include_company: bool = False,
) -> Dict[str, Any]:
    """Build canonical functional sections from an AccessScopeTableset payload."""

    ignored = set(ignore_fields)
    if include_company:
        ignored.discard("Company")

    core_rows = tableset.get("AccessScope", []) if isinstance(tableset, Mapping) else []
    entity_rows = tableset.get("AccessScopeEntity", []) if isinstance(tableset, Mapping) else []
    method_rows = tableset.get("AccessScopeBOMethod", []) if isinstance(tableset, Mapping) else []

    if not isinstance(core_rows, list):
        core_rows = []
    if not isinstance(entity_rows, list):
        entity_rows = []
    if not isinstance(method_rows, list):
        method_rows = []

    scope_id = ""
    company = ""
    if core_rows:
        first = core_rows[0]
        if isinstance(first, Mapping):
            scope_id = str(first.get("AccessScopeID") or "")
            company = str(first.get("Company") or "")

    return {
        "scope": scope_id,
        "env": "",
        "company": company,
        "core_error": "",
        "entity_error": "",
        "bom_error": "",
        "core_raw_count": len(core_rows),
        "entity_raw_count": len(entity_rows),
        "bom_raw_count": len(method_rows),
        "core": normalize_artifact_rows(core_rows, ignored),
        "entities": normalize_artifact_rows(entity_rows, ignored),
        "bo_methods": normalize_artifact_rows(method_rows, ignored),
    }


def load_scope_artifact_from_path(
    artifact_path: str,
    ignore_fields: Iterable[str] = DEFAULT_IGNORE_FIELDS,
    include_company: bool = False,
) -> Dict[str, Any]:
    """Load a scope artifact from JSON report or .eas zip package."""

    raw_path = str(artifact_path or "").strip()
    if not raw_path:
        raise ValueError("artifact_path is required")
    path = os.path.abspath(raw_path)

    _, ext = os.path.splitext(path)
    ext = ext.lower()

    if ext == ".eas":
        with zipfile.ZipFile(path, "r") as archive:
            names = {name.lower(): name for name in archive.namelist()}
            entry = names.get("accessscopetableset")
            if not entry:
                raise ValueError(".eas file is missing AccessScopeTableset entry")
            with archive.open(entry, "r") as handle:
                payload = json.loads(handle.read().decode("utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("AccessScopeTableset content is not a JSON object")
        return build_scope_artifact_from_tableset(payload, ignore_fields=ignore_fields, include_company=include_company)

    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, Mapping):
        raise ValueError("Artifact JSON root must be an object")

    if all(section in payload for section in ("AccessScope", "AccessScopeEntity", "AccessScopeBOMethod")):
        return build_scope_artifact_from_tableset(payload, ignore_fields=ignore_fields, include_company=include_company)

    return extract_artifact_payload(payload, ("core", "entities", "bo_methods"))