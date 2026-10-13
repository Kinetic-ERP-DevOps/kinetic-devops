"""
kinetic_devops/access_scope.py

Access scope and API key migration operations.

This module intentionally owns scope/API migration workflows and keeps them
separate from MetaFX layer tooling.
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

from kinetic_devops import _lazy_import

requests = _lazy_import("requests")

if __package__:
    from .artifact_validation import (
        DEFAULT_IGNORE_FIELDS as SCOPE_COMPARE_IGNORE_FIELDS,
        load_scope_artifact_from_path,
        compare_artifact_sections,
        normalize_artifact_rows,
    )
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.artifact_validation import (
        DEFAULT_IGNORE_FIELDS as SCOPE_COMPARE_IGNORE_FIELDS,
        load_scope_artifact_from_path,
        compare_artifact_sections,
        normalize_artifact_rows,
    )
    from kinetic_devops.base_client import KineticBaseClient


class KineticAccessScopeService(KineticBaseClient):
    def __init__(
        self,
        env_nickname: Optional[str] = None,
        user_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ):
        super().__init__(env_nickname=env_nickname, user_id=user_id, company_id=company_id, debug=False)

    def _odata_url(self, service: str, entity: str) -> str:
        base_url = self.config["url"].rstrip("/")
        company = self.config["company"]
        return f"{base_url}/api/v2/odata/{company}/{service}/{entity}"

    def _svc_updateext_url(self, service: str) -> str:
        base_url = self.config["url"].rstrip("/")
        company = self.config["company"]
        return f"{base_url}/api/v2/odata/{company}/{service}/UpdateExt"

    def _svc_method_url(self, service: str, method: str) -> str:
        base_url = self.config["url"].rstrip("/")
        company = self.config["company"]
        return f"{base_url}/api/v2/odata/{company}/{service}/{method}"

    @staticmethod
    def _normalize_import_relative_path(file_name: str) -> str:
        name = str(file_name or "").replace("/", "\\").lstrip("\\")
        return f"Import\\{name}"

    @staticmethod
    def _normalize_import_server_path(file_name: str) -> str:
        name = str(file_name or "").replace("\\", "/").lstrip("/")
        return f"Import//{name}"

    def upload_import_file(self, local_path: str, folder: int = 4) -> Dict[str, Any]:
        resolved_path = os.path.abspath(str(local_path or ""))
        if not os.path.isfile(resolved_path):
            raise FileNotFoundError(f"Import file not found: {local_path}")

        file_name = os.path.basename(resolved_path)
        server_path = self._normalize_import_server_path(file_name)
        encoded_data = base64.b64encode(open(resolved_path, "rb").read()).decode("ascii")

        payload = {
            "folder": int(folder),
            "serverPath": server_path,
            "data": encoded_data,
        }
        headers = self._scope_update_headers()
        url = self._svc_method_url("Ice.Lib.FileTransferSvc", "UploadFile")
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()

        body = resp.json() if resp.content else {}
        return {
            "status": resp.status_code,
            "server_path": server_path,
            "relative_path": self._normalize_import_relative_path(file_name),
            "body": body,
        }

    def import_access_scope_from_file(
        self,
        file_relative_path: str,
        override_existing: bool = True,
        new_access_scope_id: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "fileRelativePath": str(file_relative_path or ""),
            "overrideExisting": bool(override_existing),
            "newAccessScopeID": str(new_access_scope_id or ""),
        }
        headers = self._scope_update_headers()
        url = self._svc_method_url("Ice.BO.AccessScopeSvc", "ImportAccessScopeFromFile")
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        body = resp.json() if resp.content else {}

        if not resp.ok:
            error_message = ""
            if isinstance(body, dict):
                error_message = str(body.get("ErrorMessage") or body.get("ReasonPhrase") or "")
            raise RuntimeError(f"ImportAccessScopeFromFile failed ({resp.status_code}): {error_message or resp.text[:500]}")

        log_result = ""
        if isinstance(body, dict):
            params = body.get("parameters", {})
            if isinstance(params, dict):
                log_result = str(params.get("logResult") or "")

        return {
            "status": resp.status_code,
            "body": body,
            "log_result": log_result,
        }

    def export_access_scope(self, access_scope_id: str) -> Dict[str, Any]:
        scope_id = str(access_scope_id or "").strip()
        if not scope_id:
            raise ValueError("access_scope_id is required")

        payload = {"accessScopeID": scope_id}
        headers = self._scope_update_headers()
        url = self._svc_method_url("Ice.BO.AccessScopeSvc", "ExportAccessScope")
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        body = resp.json() if resp.content else {}

        encoded = ""
        log_result = ""
        if isinstance(body, dict):
            encoded = str(body.get("returnObj") or "")
            params = body.get("parameters", {})
            if isinstance(params, dict):
                raw_log = params.get("logResult")
                if isinstance(raw_log, list):
                    log_result = "\n".join(str(item) for item in raw_log)
                else:
                    log_result = str(raw_log or "")

        if not encoded:
            raise RuntimeError("ExportAccessScope returned no file data.")

        try:
            file_bytes = base64.b64decode(encoded)
        except Exception as exc:
            raise RuntimeError(f"Failed to decode export payload: {exc}") from exc

        return {
            "status": resp.status_code,
            "bytes": file_bytes,
            "log_result": log_result,
            "scope_id": scope_id,
        }

    def _scope_update_headers(self) -> Dict[str, str]:
        headers = self.mgr.get_auth_headers(self.config)
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["x-epi-request-etag"] = "true"
        headers["x-epi-extension-serialization"] = "full-metadata"
        return headers

    def fetch_scope_dataset(self, scope_id: str, session: Optional[requests.Session] = None, contextheader: str = "") -> Dict[str, Any]:
        scope_id = str(scope_id or "").strip()
        escaped_scope = scope_id.replace("'", "''")
        params = {
            "whereClauseAccessScope": f"AccessScopeID = '{escaped_scope}'",
            "whereClauseAccessScopeEntity": "",
            "whereClauseAccessScopeBOMethod": "",
            "pageSize": 0,
            "absolutePage": 1,
        }
        headers = self._scope_update_headers()
        headers["Accept"] = "application/json, text/plain, */*"
        if contextheader:
            headers["contextheader"] = contextheader

        http = session or requests.Session()
        resp = http.get(self._svc_method_url("Ice.BO.AccessScopeSvc", "GetRows"), headers=headers, params=params, timeout=60)
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        return {
            "body": body,
            "contextheader": resp.headers.get("contextheader", ""),
            "session": http,
        }

    def fetch_service_method_list(
        self,
        service_id: str,
        method_filter: str = "",
        session: Optional[requests.Session] = None,
        contextheader: str = "",
    ) -> Dict[str, Any]:
        headers = self._scope_update_headers()
        headers["Accept"] = "application/json, text/plain, */*"
        if contextheader:
            headers["contextheader"] = contextheader

        http = session or requests.Session()
        resp = http.post(
            self._svc_method_url("Ice.BO.AccessScopeSvc", "GetServiceMethodList"),
            headers=headers,
            json={"service": str(service_id or ""), "methodFilter": str(method_filter or "")},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        return {
            "body": body,
            "contextheader": resp.headers.get("contextheader", contextheader),
            "session": http,
        }

    def access_scope_exists(self, scope_id: str) -> bool:
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            return False

        url = self._odata_url("Ice.BO.AccessScopeSvc", "AccessScopes")
        escaped = scope_id.replace("'", "''")
        params = {
            "$filter": f"AccessScopeID eq '{escaped}'",
            "$top": 1,
        }
        headers = self.mgr.get_auth_headers(self.config)

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if not resp.ok:
            return False
        payload = resp.json() if resp.content else {}
        rows = payload.get("value", []) if isinstance(payload, dict) else []
        return bool(rows)

    def fetch_api_keys(self, access_scope_id: str = "") -> List[Dict[str, Any]]:
        url = self._odata_url("Ice.BO.APIKeySvc", "APIKeys")
        params: Dict[str, Any] = {
            "$top": 5000,
        }

        access_scope_id = str(access_scope_id or "").strip()
        if access_scope_id:
            escaped = access_scope_id.replace("'", "''")
            params["$filter"] = f"AccessScopeID eq '{escaped}'"

        headers = self.mgr.get_auth_headers(self.config)
        resp = requests.get(url, headers=headers, params=params, timeout=45)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        rows = payload.get("value", []) if isinstance(payload, dict) else []
        return rows if isinstance(rows, list) else []

    def _sanitize_update_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        clean: Dict[str, Any] = {}
        for key, value in record.items():
            if str(key).startswith("@"):
                continue
            clean[key] = value
        return clean

    def update_api_key_scope(self, record: Dict[str, Any], new_scope: str) -> bool:
        update_record = self._sanitize_update_record(record)
        update_record["AccessScopeID"] = str(new_scope or "")
        update_record["RowMod"] = "U"

        url = self._svc_updateext_url("Ice.BO.APIKeySvc")
        headers = self.mgr.get_auth_headers(self.config)
        headers["Content-Type"] = "application/json"

        payload = {
            "ds": {
                "APIKey": [update_record],
            },
            "continueProcessingOnError": False,
            "rollbackParentOnChildError": True,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        resp.raise_for_status()

        body = resp.json() if resp.content else {}
        if isinstance(body, dict) and body.get("errorsOccurred"):
            return False
        return True

    def get_api_key_by_id_and_company(self, key_id: str, company: str) -> Optional[Dict[str, Any]]:
        key_id = str(key_id or "").strip()
        company = str(company or "").strip()
        if not key_id:
            return None

        escaped_key = key_id.replace("'", "''")
        url = self._odata_url("Ice.BO.APIKeySvc", "APIKeys")
        if company:
            escaped_company = company.replace("'", "''")
            filter_expr = f"KeyID eq '{escaped_key}' and Company eq '{escaped_company}'"
        else:
            filter_expr = f"KeyID eq '{escaped_key}'"
        params = {
            "$filter": filter_expr,
            "$top": 1,
        }
        headers = self.mgr.get_auth_headers(self.config)

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        rows = payload.get("value", []) if isinstance(payload, dict) else []
        if not rows:
            return None
        return rows[0]

    def verify_scope_assignment(self, key_id: str, expected_scope: str, company: str = "") -> bool:
        row = self.get_api_key_by_id_and_company(key_id, company)
        if not row:
            return False
        actual_scope = str(row.get("AccessScopeID") or "")
        return actual_scope == str(expected_scope or "")


def _fetch_scope_entity_set(
    service: KineticAccessScopeService,
    scope_id: str,
    entity_set: str,
) -> Dict[str, Any]:
    scope_id = str(scope_id or "").strip()
    escaped_scope = scope_id.replace("'", "''")
    url = service._odata_url("Ice.BO.AccessScopeSvc", entity_set)
    params = {
        "$filter": f"AccessScopeID eq '{escaped_scope}'",
        "$top": 5000,
    }
    headers = service.mgr.get_auth_headers(service.config)

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=60)
    except Exception as exc:
        return {"error": str(exc), "rows": []}

    if not resp.ok:
        return {"error": f"{resp.status_code} {resp.text[:200]}", "rows": []}

    payload = resp.json() if resp.content else {}
    rows = payload.get("value", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        rows = []
    return {"error": "", "rows": rows}


def build_scope_functional_artifact(service: KineticAccessScopeService, scope_id: str) -> Dict[str, Any]:
    core = _fetch_scope_entity_set(service, scope_id, "AccessScopes")
    entities = _fetch_scope_entity_set(service, scope_id, "AccessScopeEntities")
    bo_methods = _fetch_scope_entity_set(service, scope_id, "AccessScopeBOMethods")

    return {
        "scope": scope_id,
        "env": service.config.get("nickname", ""),
        "company": service.config.get("company", ""),
        "core_error": core["error"],
        "entity_error": entities["error"],
        "bom_error": bo_methods["error"],
        "core_raw_count": len(core["rows"]),
        "entity_raw_count": len(entities["rows"]),
        "bom_raw_count": len(bo_methods["rows"]),
        "core_rows": list(core["rows"]),
        "entity_rows": list(entities["rows"]),
        "bo_method_rows": list(bo_methods["rows"]),
        "core": normalize_artifact_rows(core["rows"], SCOPE_COMPARE_IGNORE_FIELDS),
        "entities": normalize_artifact_rows(entities["rows"], SCOPE_COMPARE_IGNORE_FIELDS),
        "bo_methods": normalize_artifact_rows(bo_methods["rows"], SCOPE_COMPARE_IGNORE_FIELDS),
    }


def compare_scope_functional_artifacts(reference: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    comparison = compare_artifact_sections(reference, target, ("core", "entities", "bo_methods"))
    section_error_fields = {
        "core": "core_error",
        "entities": "entity_error",
        "bo_methods": "bom_error",
    }

    for section_name, error_field in section_error_fields.items():
        if reference.get(error_field) or target.get(error_field):
            comparison[f"{section_name}_identical"] = False

    comparison["functionally_identical"] = all(
        comparison[f"{section_name}_identical"] for section_name in ("core", "entities", "bo_methods")
    )
    return comparison


def _section_delta_rows(reference_rows: List[Dict[str, Any]], target_rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    reference_map = {
        json.dumps(row, sort_keys=True, default=str): row
        for row in reference_rows
    }
    target_map = {
        json.dumps(row, sort_keys=True, default=str): row
        for row in target_rows
    }

    reference_keys = sorted(reference_map)
    target_keys = sorted(target_map)
    return {
        "missing": [reference_map[key] for key in reference_keys if key not in target_map],
        "extra": [target_map[key] for key in target_keys if key not in reference_map],
    }


def _scope_section_row_key(section: str, row: Dict[str, Any]) -> str:
    if section == "entities":
        return "|".join([
            str(row.get("EntityType") or "").strip(),
            str(row.get("EntityID") or "").strip(),
        ])
    if section == "bo_methods":
        return "|".join([
            str(row.get("EntityType") or "").strip(),
            str(row.get("EntityID") or "").strip(),
            str(row.get("MethodID") or "").strip(),
        ])
    return str(row.get("AccessScopeID") or "").strip()


def _comparable_scope_section_row(row: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in row.items():
        if str(key).startswith("@") or key in SCOPE_COMPARE_IGNORE_FIELDS:
            continue
        clean[key] = value
    return clean


def _section_sync_plan(
    section: str,
    reference_rows: List[Dict[str, Any]],
    target_rows: List[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    reference_map: Dict[str, Dict[str, Any]] = {}
    target_map: Dict[str, Dict[str, Any]] = {}

    for row in reference_rows:
        key = _scope_section_row_key(section, row)
        if key:
            reference_map[key] = row

    for row in target_rows:
        key = _scope_section_row_key(section, row)
        if key:
            target_map[key] = row

    missing: List[Dict[str, Any]] = []
    updates: List[Dict[str, Any]] = []
    extra: List[Dict[str, Any]] = []

    for row in reference_rows:
        key = _scope_section_row_key(section, row)
        target_row = target_map.get(key)
        if target_row is None:
            missing.append(row)
            continue
        if _comparable_scope_section_row(row) != _comparable_scope_section_row(target_row):
            updates.append(row)

    for row in target_rows:
        key = _scope_section_row_key(section, row)
        if key not in reference_map:
            extra.append(row)

    return {
        "missing": missing,
        "updates": updates,
        "extra": extra,
    }


def _scope_entity_key(row: Dict[str, Any]) -> str:
    return _scope_section_row_key("entities", row)


def _scope_method_parent_key(row: Dict[str, Any]) -> str:
    return _scope_entity_key(row)


def _parent_entity_rows_for_methods(
    method_rows: List[Dict[str, Any]],
    reference_entities: List[Dict[str, Any]],
    target_entities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    reference_map = {
        _scope_entity_key(row): row
        for row in reference_entities
        if _scope_entity_key(row)
    }
    target_map = {
        _scope_entity_key(row): row
        for row in target_entities
        if _scope_entity_key(row)
    }

    parent_rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in method_rows:
        parent_key = _scope_method_parent_key(row)
        if not parent_key or parent_key in seen:
            continue
        seen.add(parent_key)

        parent_row = reference_map.get(parent_key) or target_map.get(parent_key)
        if parent_row is None:
            continue
        parent_rows.append(parent_row)

    return parent_rows


def _sanitize_scope_section_row(record: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in record.items():
        if str(key).startswith("@"):  # response metadata, never post back
            continue
        if key in {
            "RowMod",
            "CreatedBy",
            "CreatedOn",
            "ChangedBy",
            "ChangedOn",
            "LastUpdated",
            "LastUpdatedBy",
        }:
            continue
        clean[key] = value
    return clean


def _build_scope_update_payload_row(
    source_row: Dict[str, Any],
    row_mod: str,
    target_row: Optional[Dict[str, Any]] = None,
    company_id: str = "",
) -> Dict[str, Any]:
    if row_mod == "A":
        clean = _sanitize_scope_section_row(source_row)
        clean.pop("SysRevID", None)
        clean.pop("SysRowID", None)
        clean["BitFlag"] = clean.get("BitFlag", 0)
        if company_id and not clean.get("Company"):
            clean["Company"] = company_id
        clean["RowMod"] = "A"
        return clean

    if target_row is None:
        raise ValueError(f"target_row is required for RowMod={row_mod}")

    base = _sanitize_scope_section_row(target_row)
    source_clean = _sanitize_scope_section_row(source_row)
    for key, value in source_clean.items():
        if key in {"SysRevID", "SysRowID", "BitFlag", "Company"}:
            continue
        base[key] = value
    base.pop("SysRowID", None)
    if company_id and not base.get("Company"):
        base["Company"] = company_id
    base["RowMod"] = row_mod
    return base


def _scope_update_has_errors(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("errorsOccurred"):
        return True

    parameters = body.get("parameters")
    if isinstance(parameters, dict) and parameters.get("errorsOccurred"):
        return True

    return_obj = body.get("returnObj")
    if isinstance(return_obj, dict):
        bo_errors = return_obj.get("BOUpdError")
        if isinstance(bo_errors, list) and bo_errors:
            return True

    return False


def _post_scope_updateext(
    service: KineticAccessScopeService,
    entity_set: str,
    add_rows: List[Dict[str, Any]],
    update_rows: List[Dict[str, Any]],
    delete_rows: List[Dict[str, Any]],
) -> bool:
    payload_rows: List[Dict[str, Any]] = []
    for row in add_rows:
        clean = _sanitize_scope_section_row(row)
        clean["RowMod"] = "A"
        payload_rows.append(clean)
    for row in update_rows:
        clean = _sanitize_scope_section_row(row)
        clean["RowMod"] = "U"
        payload_rows.append(clean)
    for row in delete_rows:
        clean = _sanitize_scope_section_row(row)
        clean["RowMod"] = "D"
        payload_rows.append(clean)

    if not payload_rows:
        return True

    url = service._svc_updateext_url("Ice.BO.AccessScopeSvc")
    headers = service._scope_update_headers()

    payload = {
        "ds": {
            entity_set: payload_rows,
        },
        "continueProcessingOnError": False,
        "rollbackParentOnChildError": True,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()

    body = resp.json() if resp.content else {}
    if isinstance(body, dict) and body.get("errorsOccurred"):
        return False
    return True


def _post_scope_updateext_batch(
    service: KineticAccessScopeService,
    rows_by_entity_set: Dict[str, Dict[str, List[Dict[str, Any]]]],
    access_scope_rows: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    payload_sets = {
        entity_set: [row for rows in plan.values() for row in rows]
        for entity_set, plan in rows_by_entity_set.items()
        if any(plan.values())
    }

    if not payload_sets:
        return True

    url = service._svc_updateext_url("Ice.BO.AccessScopeSvc")
    headers = service._scope_update_headers()

    payload = {
        "ds": {
            "AccessScope": access_scope_rows or [],
            **payload_sets,
        },
        "continueProcessingOnError": False,
        "rollbackParentOnChildError": True,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()

    body = resp.json() if resp.content else {}
    if _scope_update_has_errors(body):
        return False
    return True


def _post_scope_updateext_row(
    service: KineticAccessScopeService,
    entity_set: str,
    payload_row: Dict[str, Any],
    access_scope_rows: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    return _post_scope_updateext_batch(
        service,
        {
            entity_set: {
                "rows": [payload_row],
            }
        },
        access_scope_rows=access_scope_rows,
    )


def rebuild_scope_bo_methods(reference: Dict[str, Any], target_service: KineticAccessScopeService, scope_id: str) -> Dict[str, Any]:
    reference_entities = list(reference.get("entity_rows", reference.get("entities", [])))
    reference_rows = list(reference.get("bo_method_rows", reference.get("bo_methods", [])))
    target_artifact = build_scope_functional_artifact(target_service, scope_id)
    target_core_rows = list(target_artifact.get("core_rows", target_artifact.get("core", [])))
    target_entities = list(target_artifact.get("entity_rows", target_artifact.get("entities", [])))
    target_rows = list(target_artifact.get("bo_method_rows", target_artifact.get("bo_methods", [])))
    entity_plan = _section_sync_plan("entities", reference_entities, target_entities)
    delta = _section_sync_plan("bo_methods", reference_rows, target_rows)

    applied = {
        "entity_updates": 0,
        "method_updates": 0,
        "deleted": 0,
        "added": 0,
        "deleted_rows": [],
        "added_rows": [],
        "applied": False,
    }

    entity_add_rows = list(entity_plan["missing"])
    entity_update_rows = list(entity_plan["updates"])
    entity_delete_rows = list(entity_plan["extra"])

    method_add_rows = list(delta["missing"])
    method_update_rows = list(delta["updates"])
    method_delete_rows = list(delta["extra"])

    if not any((entity_add_rows, entity_update_rows, entity_delete_rows, method_add_rows, method_update_rows, method_delete_rows)):
        return {
            "entity_delta": entity_plan,
            "delta": delta,
            "applied": False,
            "deleted": 0,
            "added": 0,
            "updated": 0,
            "verified": True,
        }

    target_entity_keys = {_scope_entity_key(row) for row in target_entities}
    reference_entity_map = {
        _scope_entity_key(row): row
        for row in reference_entities
        if _scope_entity_key(row)
    }
    target_entity_map = {
        _scope_entity_key(row): row
        for row in target_entities
        if _scope_entity_key(row)
    }
    target_method_map = {
        _scope_section_row_key("bo_methods", row): row
        for row in target_rows
        if _scope_section_row_key("bo_methods", row)
    }
    company_id = str(target_service.config.get("company") or "")
    access_scope_rows = []
    if target_core_rows:
        access_scope_rows.append(
            _build_scope_update_payload_row(
                target_core_rows[0],
                "U",
                target_row=target_core_rows[0],
                company_id=company_id,
            )
        )

    for row in entity_delete_rows:
        payload_row = _build_scope_update_payload_row(
            row,
            "D",
            target_row=target_entity_map.get(_scope_entity_key(row)),
            company_id=company_id,
        )
        if not _post_scope_updateext_row(target_service, "AccessScopeEntity", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 1,
                "added": 0,
                "updated": 0,
                "verified": False,
            }

    for row in entity_update_rows:
        payload_row = _build_scope_update_payload_row(
            row,
            "U",
            target_row=target_entity_map.get(_scope_entity_key(row)),
            company_id=company_id,
        )
        if not _post_scope_updateext_row(target_service, "AccessScopeEntity", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 0,
                "added": 0,
                "updated": 1,
                "verified": False,
            }

    for row in entity_add_rows:
        payload_row = _build_scope_update_payload_row(row, "A", company_id=company_id)
        if not _post_scope_updateext_row(target_service, "AccessScopeEntity", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 0,
                "added": 1,
                "updated": 0,
                "verified": False,
            }

    for row in method_delete_rows:
        payload_row = _build_scope_update_payload_row(
            row,
            "D",
            target_row=target_method_map.get(_scope_section_row_key("bo_methods", row)),
            company_id=company_id,
        )
        if not _post_scope_updateext_row(target_service, "AccessScopeBOMethod", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 1,
                "added": 0,
                "updated": 0,
                "verified": False,
            }

    for row in method_update_rows:
        parent_key = _scope_method_parent_key(row)
        if parent_key and parent_key not in target_entity_keys:
            parent_row = reference_entity_map.get(parent_key)
            if parent_row:
                parent_payload = _build_scope_update_payload_row(parent_row, "A", company_id=company_id)
            else:
                parent_payload = None
            if parent_payload and not _post_scope_updateext_row(target_service, "AccessScopeEntity", parent_payload, access_scope_rows=access_scope_rows):
                return {
                    "entity_delta": entity_plan,
                    "delta": delta,
                    "applied": False,
                    "deleted": 0,
                    "added": 1,
                    "updated": 0,
                    "verified": False,
                }
            target_entity_keys.add(parent_key)

        payload_row = _build_scope_update_payload_row(
            row,
            "U",
            target_row=target_method_map.get(_scope_section_row_key("bo_methods", row)),
            company_id=company_id,
        )
        if not _post_scope_updateext_row(target_service, "AccessScopeBOMethod", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 0,
                "added": 0,
                "updated": 1,
                "verified": False,
            }

    for row in method_add_rows:
        parent_key = _scope_method_parent_key(row)
        if parent_key and parent_key not in target_entity_keys:
            parent_row = reference_entity_map.get(parent_key)
            if parent_row:
                parent_payload = _build_scope_update_payload_row(parent_row, "A", company_id=company_id)
            else:
                parent_payload = None
            if parent_payload and not _post_scope_updateext_row(target_service, "AccessScopeEntity", parent_payload, access_scope_rows=access_scope_rows):
                return {
                    "entity_delta": entity_plan,
                    "delta": delta,
                    "applied": False,
                    "deleted": 0,
                    "added": 1,
                    "updated": 0,
                    "verified": False,
                }
            target_entity_keys.add(parent_key)

        payload_row = _build_scope_update_payload_row(row, "A", company_id=company_id)
        if not _post_scope_updateext_row(target_service, "AccessScopeBOMethod", payload_row, access_scope_rows=access_scope_rows):
            return {
                "entity_delta": entity_plan,
                "delta": delta,
                "applied": False,
                "deleted": 0,
                "added": 1,
                "updated": 0,
                "verified": False,
            }

    refreshed = build_scope_functional_artifact(target_service, scope_id)
    verified = compare_scope_functional_artifacts(reference, refreshed)["bo_methods_identical"]
    return {
        "entity_delta": entity_plan,
        "delta": delta,
        "applied": True,
        "deleted": len(entity_delete_rows) + len(method_delete_rows),
        "added": len(entity_add_rows) + len(method_add_rows),
        "updated": len(entity_update_rows) + len(method_update_rows),
        "verified": verified,
        "post_refresh": refreshed,
    }


def _write_validation_report(service: KineticAccessScopeService, report: Dict[str, Any], report_path: str) -> str:
    if not report_path:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = os.path.join("exports", "System-Apps", "AccessScopeMigr", f"scope_validation_report_{ts}.json")

    out_dir = os.path.dirname(report_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    resolved = service.resolve_output_path(report_path, conflict_resolution="timestamp")
    with open(resolved, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return resolved


def run_scope_validation(args: argparse.Namespace) -> int:
    target = KineticAccessScopeService(
        env_nickname=args.target_env,
        user_id=args.target_user,
        company_id=args.target_company,
    )

    try:
        reference_artifact: Dict[str, Any]

        if args.reference_artifact:
            reference_artifact = load_scope_artifact_from_path(args.reference_artifact, ignore_fields=SCOPE_COMPARE_IGNORE_FIELDS)
        else:
            reference = KineticAccessScopeService(
                env_nickname=args.reference_env,
                user_id=args.reference_user,
                company_id=args.reference_company,
            )
            reference_artifact = build_scope_functional_artifact(reference, args.scope_id)

            if args.export_reference:
                out_dir = os.path.dirname(args.export_reference)
                if out_dir:
                    os.makedirs(out_dir, exist_ok=True)
                with open(args.export_reference, "w", encoding="utf-8") as f:
                    json.dump(reference_artifact, f, indent=2, ensure_ascii=False)

        target_artifact = build_scope_functional_artifact(target, args.scope_id)
        comparison = compare_scope_functional_artifacts(reference_artifact, target_artifact)

        report = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": args.scope_id,
            "comparison_mode": "functional-scope-only (ignores api key/user assignments)",
            "reference": reference_artifact,
            "target": target_artifact,
            **comparison,
        }

        report_path = _write_validation_report(target, report, args.report)
        print(report_path)
        print(f"functionally_identical={comparison['functionally_identical']}")
        print(f"core_identical={comparison['core_identical']}")
        print(f"entities_identical={comparison['entities_identical']}")
        print(f"bo_methods_identical={comparison['bo_methods_identical']}")

        if not comparison["functionally_identical"] and not args.allow_drift:
            return 1
        return 0
    except Exception as exc:
        print(f"Scope validation failed: {exc}")
        return 1


def _resolve_reference_scope_artifact(args: argparse.Namespace) -> Dict[str, Any]:
    if str(getattr(args, "reference_artifact", "") or "").strip():
        return load_scope_artifact_from_path(args.reference_artifact, ignore_fields=SCOPE_COMPARE_IGNORE_FIELDS)

    reference = KineticAccessScopeService(
        env_nickname=args.reference_env,
        user_id=getattr(args, "reference_user", ""),
        company_id=getattr(args, "reference_company", ""),
    )
    return build_scope_functional_artifact(reference, args.scope_id)


def _write_scope_refresh_report(service: KineticAccessScopeService, report: Dict[str, Any], report_path: str) -> str:
    if not report_path:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = os.path.join("exports", "System-Apps", "AccessScopeMigr", f"scope_refresh_import_report_{ts}.json")

    out_dir = os.path.dirname(report_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    resolved = service.resolve_output_path(report_path, conflict_resolution="timestamp")
    with open(resolved, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return resolved


def run_scope_refresh_import(args: argparse.Namespace) -> int:
    target = KineticAccessScopeService(
        env_nickname=args.target_env,
        user_id=args.target_user,
        company_id=args.target_company,
    )

    report: Dict[str, Any] = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": args.scope_id,
        "target_env": target.config.get("nickname", args.target_env),
        "comparison_mode": "functional-scope-only (ignores api key/user assignments)",
        "dry_run": bool(args.dry_run),
        "steps": [],
        "failures": [],
    }

    try:
        reference_artifact = _resolve_reference_scope_artifact(args)
        target_before = build_scope_functional_artifact(target, args.scope_id)
        pre_compare = compare_scope_functional_artifacts(reference_artifact, target_before)
        report["pre_refresh"] = pre_compare

        attached_rows = target.fetch_api_keys(args.scope_id)
        attached_keys = [str(row.get("KeyID") or "").strip() for row in attached_rows if str(row.get("KeyID") or "").strip()]
        report["steps"].append(
            {
                "step": "discover-target-keys",
                "count": len(attached_keys),
                "key_ids": attached_keys,
            }
        )

        if args.dry_run:
            report["steps"].append(
                {
                    "step": "plan-refresh-import",
                    "detach_required": bool(attached_keys),
                    "manual_pause": bool(args.pause_for_import),
                    "has_import_command": bool(str(args.import_command or "").strip()),
                }
            )
            output = _write_scope_refresh_report(target, report, args.report)
            print(output)
            print(f"pre_functionally_identical={pre_compare['functionally_identical']}")
            print(f"attached_keys={len(attached_keys)}")
            return 0

        detached: List[str] = []
        detached_rows: List[Dict[str, Any]] = []
        rollback_attempted = False

        def restore_detached_rows() -> None:
            nonlocal rollback_attempted
            if rollback_attempted or not detached_rows:
                return
            rollback_attempted = True

            for original_row in detached_rows:
                key_id = str(original_row.get("KeyID") or "").strip()
                if not key_id:
                    continue
                company = str(original_row.get("Company") or "").strip()
                original_scope = str(original_row.get("AccessScopeID") or "").strip()
                step: Dict[str, Any] = {
                    "step": "restore-detached-scope",
                    "key_id": key_id,
                    "scope_id": original_scope,
                }
                try:
                    current_row = target.get_api_key_by_id_and_company(key_id, company) or original_row
                    ok = target.update_api_key_scope(current_row, original_scope)
                    step["ok"] = ok
                    if not ok:
                        report["failures"].append(f"restore failed for {key_id}")
                except Exception as restore_exc:
                    step["ok"] = False
                    step["error"] = str(restore_exc)
                    report["failures"].append(f"restore failed for {key_id}: {restore_exc}")
                report["steps"].append(step)

        def fail_refresh(failure: str, message: str) -> int:
            report["failures"].append(failure)
            restore_detached_rows()
            output = _write_scope_refresh_report(target, report, args.report)
            print(f"{message}. Report: {output}")
            return 1

        for row in attached_rows:
            key_id = str(row.get("KeyID") or "").strip()
            if not key_id:
                continue
            ok = target.update_api_key_scope(row, "")
            report["steps"].append({"step": "detach-scope", "key_id": key_id, "ok": ok})
            if not ok:
                return fail_refresh(f"detach failed for {key_id}", "Detach phase failed")
            detached.append(key_id)
            detached_rows.append(dict(row))

        import_eas = str(getattr(args, "import_eas", "") or "").strip()
        import_command = str(args.import_command or "").strip()
        if import_eas:
            uploaded = target.upload_import_file(import_eas)
            report["steps"].append(
                {
                    "step": "upload-import-file",
                    "local_path": os.path.abspath(import_eas),
                    "server_path": uploaded.get("server_path", ""),
                    "file_relative_path": uploaded.get("relative_path", ""),
                    "status": uploaded.get("status", 0),
                }
            )

            imported = target.import_access_scope_from_file(
                uploaded.get("relative_path", ""),
                override_existing=bool(getattr(args, "override_existing_scope", False)),
                new_access_scope_id=str(getattr(args, "new_access_scope_id", "") or args.scope_id),
            )
            log_result = str(imported.get("log_result") or "")
            report["steps"].append(
                {
                    "step": "import-access-scope-from-file",
                    "file_relative_path": uploaded.get("relative_path", ""),
                    "override_existing": bool(getattr(args, "override_existing_scope", False)),
                    "status": imported.get("status", 0),
                    "log_result": log_result,
                }
            )

            if "already exists" in log_result.lower() and not bool(getattr(args, "override_existing_scope", False)):
                return fail_refresh(
                    "Import did not overwrite existing scope. Re-run with override enabled.",
                    "Import step blocked by existing scope",
                )
        elif import_command:
            completed = subprocess.run(import_command, shell=True, text=True)
            report["steps"].append(
                {
                    "step": "run-import-command",
                    "command": import_command,
                    "returncode": completed.returncode,
                }
            )
            if completed.returncode != 0:
                return fail_refresh(f"Import command failed with code {completed.returncode}", "Import command failed")
        elif args.pause_for_import:
            print("\nPerform the supported scope import now, then press Enter to continue with validation and key rebind.")
            input("Press Enter after import completes: ")
            report["steps"].append({"step": "manual-import-pause-complete"})
        else:
            return fail_refresh(
                "No import step specified. Use --import-eas, --import-command, or --pause-for-import.",
                "No import step specified",
            )

        if not target.access_scope_exists(args.scope_id):
            return fail_refresh(
                f"Target scope '{args.scope_id}' not found after import.",
                f"Target scope '{args.scope_id}' not found after import",
            )

        target_after_import = build_scope_functional_artifact(target, args.scope_id)
        post_compare = compare_scope_functional_artifacts(reference_artifact, target_after_import)
        report["post_import"] = post_compare

        rebound: List[str] = []
        for row in attached_rows:
            key_id = str(row.get("KeyID") or "").strip()
            if not key_id:
                continue
            company = str(row.get("Company") or "").strip()
            current_row = target.get_api_key_by_id_and_company(key_id, company) or row
            ok = target.update_api_key_scope(current_row, args.scope_id)
            report["steps"].append({"step": "reattach-scope", "key_id": key_id, "ok": ok})
            if not ok:
                return fail_refresh(f"reattach failed for {key_id}", "Reattach phase failed")
            rebound.append(key_id)

        failed_verify = []
        verified = []
        for row in attached_rows:
            key_id = str(row.get("KeyID") or "").strip()
            if not key_id:
                continue
            company = str(row.get("Company") or "").strip()
            key_ref = f"{company}:{key_id}" if company else key_id
            if target.verify_scope_assignment(key_id, args.scope_id, company=company):
                verified.append(key_ref)
            else:
                failed_verify.append(key_ref)
        report["steps"].append(
            {
                "step": "verify-reattach",
                "verified": verified,
                "failed": failed_verify,
            }
        )
        if failed_verify:
            return fail_refresh(
                "Re-attach verification failed for one or more keys.",
                f"Verification failed for {len(failed_verify)} keys",
            )

        output = _write_scope_refresh_report(target, report, args.report)
        print(output)
        print(f"pre_functionally_identical={pre_compare['functionally_identical']}")
        print(f"post_functionally_identical={post_compare['functionally_identical']}")
        print(f"detached_keys={len(detached)}")
        print(f"reattached_keys={len(rebound)}")
        return 0 if post_compare["functionally_identical"] or args.allow_drift else 1
    except Exception as exc:
        report["failures"].append(str(exc))
        restore_detached_rows()
        output = _write_scope_refresh_report(target, report, args.report)
        print(f"Scope refresh/import failed: {exc}. Report: {output}")
        return 1


def run_scope_export_eas(args: argparse.Namespace) -> int:
    service = KineticAccessScopeService(
        env_nickname=args.source_env,
        user_id=args.source_user,
        company_id=args.source_company,
    )
    service.configure_file_resolution_from_args(args)

    try:
        result = service.export_access_scope(args.scope_id)

        output_path = str(args.output or "").strip()
        if not output_path:
            safe_scope = re.sub(r"[^A-Za-z0-9_.-]", "_", str(args.scope_id or "").strip() or "scope")
            output_path = os.path.join("exports", "System-Apps", "AccessScopeMigr", f"AccessScope_{safe_scope}.eas")

        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        resolved_output = service.resolve_output_path(output_path, conflict_resolution=getattr(args, "file_conflict", "timestamp"))
        with open(resolved_output, "wb") as f:
            f.write(result["bytes"])

        print(resolved_output)
        print(f"bytes={len(result['bytes'])}")
        if result.get("log_result"):
            print("export_log=present")
        return 0
    except Exception as exc:
        print(f"Scope export failed: {exc}")
        return 1


def _build_scope_migration_report() -> Dict[str, Any]:
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": {},
        "steps": [],
        "failures": [],
    }


def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _source_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    for raw in (row.get("KeyID"), row.get("Name")):
        normalized = _norm_key(str(raw or ""))
        if normalized and normalized not in tokens:
            tokens.append(normalized)
    return tokens


def _resolve_target_row(
    source_row: Dict[str, Any],
    target_exact_by_keyid: Dict[str, Dict[str, Any]],
    target_rows: List[Dict[str, Any]],
    to_scope: str,
) -> tuple[Optional[Dict[str, Any]], str, List[str]]:
    src_key_id = str(source_row.get("KeyID") or "").strip()
    if src_key_id and src_key_id in target_exact_by_keyid:
        return target_exact_by_keyid[src_key_id], "exact-keyid", []

    tokens = _source_tokens(source_row)
    if not tokens:
        return None, "missing", []

    candidates: List[Dict[str, Any]] = []
    for row in target_rows:
        tgt_key = _norm_key(str(row.get("KeyID") or ""))
        tgt_name = _norm_key(str(row.get("Name") or ""))
        if any(token and (token == tgt_key or token == tgt_name) for token in tokens):
            candidates.append(row)

    if len(candidates) == 1:
        return candidates[0], "alias-normalized", []

    if len(candidates) > 1:
        scoped = [
            row for row in candidates
            if str(row.get("AccessScopeID") or "").strip() == str(to_scope or "").strip()
        ]
        if len(scoped) == 1:
            return scoped[0], "alias-normalized-scope-preferred", []
        conflict_ids = [str(row.get("KeyID") or "").strip() for row in candidates if str(row.get("KeyID") or "").strip()]
        return None, "ambiguous", conflict_ids

    return None, "missing", []


def _write_scope_migration_report(service: KineticAccessScopeService, report: Dict[str, Any], report_path: str) -> str:
    if not report_path:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        report_path = os.path.join("temp", f"access_scope_migration_report_{ts}.json")

    out_dir = os.path.dirname(report_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    resolved = service.resolve_output_path(report_path, conflict_resolution="timestamp")
    with open(resolved, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    return resolved


def run_scope_migration(args: argparse.Namespace) -> int:
    source = KineticAccessScopeService(
        env_nickname=args.source_env,
        user_id=args.source_user,
        company_id=args.source_company,
    )
    target = KineticAccessScopeService(
        env_nickname=args.target_env,
        user_id=args.target_user,
        company_id=args.target_company,
    )

    report = _build_scope_migration_report()
    report["summary"] = {
        "source_env": source.config.get("nickname", args.source_env),
        "target_env": target.config.get("nickname", args.target_env),
        "from_scope": args.from_scope,
        "to_scope": args.to_scope,
        "strategy": args.strategy,
        "dry_run": bool(args.dry_run),
    }

    try:
        source_rows = source.fetch_api_keys(args.from_scope)
        source_key_ids = sorted(
            {str(r.get("KeyID") or "").strip() for r in source_rows if str(r.get("KeyID") or "").strip()}
        )
        report["steps"].append(
            {
                "step": "discover-source-keys",
                "count": len(source_key_ids),
                "key_ids": source_key_ids,
            }
        )

        if not source_key_ids:
            report["failures"].append("No source API keys were found for from-scope.")
            output = _write_scope_migration_report(target, report, args.report)
            print(f"No source keys found for scope '{args.from_scope}'. Report: {output}")
            return 1

        target_rows = target.fetch_api_keys("")
        target_index = {
            str(r.get("KeyID") or "").strip(): r
            for r in target_rows
            if str(r.get("KeyID") or "").strip()
        }

        matched_rows: List[Dict[str, Any]] = []
        matched_target_ids: set[str] = set()
        alias_matches: List[Dict[str, str]] = []
        missing_target_keys: List[str] = []
        ambiguous_target_keys: List[Dict[str, Any]] = []

        for source_row in source_rows:
            source_key_id = str(source_row.get("KeyID") or "").strip()
            if not source_key_id:
                continue

            resolved_row, match_mode, conflicts = _resolve_target_row(
                source_row=source_row,
                target_exact_by_keyid=target_index,
                target_rows=target_rows,
                to_scope=args.to_scope,
            )

            if resolved_row is None:
                if match_mode == "ambiguous":
                    ambiguous_target_keys.append(
                        {
                            "source_key_id": source_key_id,
                            "source_name": str(source_row.get("Name") or "").strip(),
                            "conflicts": conflicts,
                        }
                    )
                else:
                    missing_target_keys.append(source_key_id)
                continue

            resolved_key_id = str(resolved_row.get("KeyID") or "").strip()
            if resolved_key_id and resolved_key_id in matched_target_ids:
                continue

            matched_rows.append(resolved_row)
            if resolved_key_id:
                matched_target_ids.add(resolved_key_id)

            if match_mode != "exact-keyid":
                alias_matches.append(
                    {
                        "source_key_id": source_key_id,
                        "source_name": str(source_row.get("Name") or "").strip(),
                        "target_key_id": resolved_key_id,
                        "target_name": str(resolved_row.get("Name") or "").strip(),
                        "mode": match_mode,
                    }
                )

        report["steps"].append(
            {
                "step": "target-candidate-match",
                "matched": len(matched_rows),
                "missing": missing_target_keys,
                "ambiguous": ambiguous_target_keys,
                "alias_matches": alias_matches,
            }
        )

        if ambiguous_target_keys:
            report["failures"].append(
                "Ambiguous source-to-target key mapping detected. Use unique target key IDs or adjust names before migration."
            )
            output = _write_scope_migration_report(target, report, args.report)
            print(f"Ambiguous key mapping detected. Report: {output}")
            return 1

        if missing_target_keys and not args.allow_missing_target_keys:
            report["failures"].append(
                "Some source API keys are missing in target environment. Re-run with --allow-missing-target-keys to continue."
            )
            output = _write_scope_migration_report(target, report, args.report)
            print(f"Missing {len(missing_target_keys)} target keys. Report: {output}")
            return 1

        if not matched_rows:
            report["failures"].append("No target API keys matched source key IDs.")
            output = _write_scope_migration_report(target, report, args.report)
            print(f"No matching target keys for migration. Report: {output}")
            return 1

        if args.strategy == "direct" and not target.access_scope_exists(args.to_scope):
            report["failures"].append(f"Target scope '{args.to_scope}' does not exist.")
            output = _write_scope_migration_report(target, report, args.report)
            print(f"Target scope '{args.to_scope}' not found. Report: {output}")
            return 1

        migrated_refs: List[Dict[str, str]] = []
        detached_refs: List[Dict[str, str]] = []

        def _apply(records: List[Dict[str, Any]], new_scope: str, phase: str) -> List[Dict[str, str]]:
            updated: List[Dict[str, str]] = []
            for row in records:
                key_id = str(row.get("KeyID") or "").strip()
                company = str(row.get("Company") or "").strip()
                if not key_id:
                    continue

                if args.dry_run:
                    updated.append({"key_id": key_id, "company": company})
                    continue

                current_row = target.get_api_key_by_id_and_company(key_id, company) or row
                ok = target.update_api_key_scope(current_row, new_scope)
                if not ok:
                    report["failures"].append(f"{phase}: failed update for KeyID={key_id}")
                    continue
                updated.append({"key_id": key_id, "company": company})
            return updated

        if args.strategy == "detach-rebind":
            detached_refs = _apply(matched_rows, "", "detach")
            report["steps"].append(
                {
                    "step": "detach-scope",
                    "updated": [f"{item['company']}:{item['key_id']}" if item["company"] else item["key_id"] for item in detached_refs],
                }
            )

            if report["failures"]:
                output = _write_scope_migration_report(target, report, args.report)
                print(f"Detach phase failed. Report: {output}")
                return 1

            if args.pause_for_import and not args.dry_run:
                print("\nPerform fresh import now, then press Enter to continue with scope rebind.")
                input("Press Enter after import completes: ")

            if args.import_command and not args.dry_run:
                print(f"\nRunning import command: {args.import_command}")
                completed = subprocess.run(args.import_command, shell=True, text=True)
                if completed.returncode != 0:
                    report["failures"].append(f"Import command failed with code {completed.returncode}")
                    output = _write_scope_migration_report(target, report, args.report)
                    print(f"Import command failed. Report: {output}")
                    return 1

            if not target.access_scope_exists(args.to_scope):
                report["failures"].append(f"Target scope '{args.to_scope}' does not exist before rebind.")
                output = _write_scope_migration_report(target, report, args.report)
                print(f"Target scope '{args.to_scope}' not found before rebind. Report: {output}")
                return 1

        migrated_refs = _apply(matched_rows, args.to_scope, "rebind" if args.strategy == "detach-rebind" else "direct")
        report["steps"].append(
            {
                "step": "assign-target-scope",
                "updated": [f"{item['company']}:{item['key_id']}" if item["company"] else item["key_id"] for item in migrated_refs],
                "scope": args.to_scope,
            }
        )

        if report["failures"]:
            output = _write_scope_migration_report(target, report, args.report)
            print(f"Scope update failed for one or more keys. Report: {output}")
            return 1

        verification_target = migrated_refs if not args.dry_run else [
            {
                "key_id": str(r.get("KeyID") or "").strip(),
                "company": str(r.get("Company") or "").strip(),
            }
            for r in matched_rows
            if str(r.get("KeyID") or "").strip()
        ]
        verified: List[str] = []
        failed_verify: List[str] = []
        if not args.dry_run:
            for item in verification_target:
                key_id = str(item.get("key_id") or "")
                company = str(item.get("company") or "")
                key_ref = f"{company}:{key_id}" if company else key_id
                if target.verify_scope_assignment(key_id, args.to_scope, company=company):
                    verified.append(key_ref)
                else:
                    failed_verify.append(key_ref)

        report["steps"].append(
            {
                "step": "verify-rebind",
                "verified": verified,
                "failed": failed_verify,
            }
        )

        if failed_verify:
            report["failures"].append(
                "Re-attach verification failed for one or more keys. Load should be treated as failed."
            )
            output = _write_scope_migration_report(target, report, args.report)
            print(f"Verification failed for {len(failed_verify)} keys. Report: {output}")
            return 1

        output = _write_scope_migration_report(target, report, args.report)
        print(f"Scope migration complete. Report: {output}")
        return 0

    except Exception as exc:
        report["failures"].append(f"Unhandled error: {exc}")
        output = _write_scope_migration_report(target, report, args.report)
        print(f"Scope migration failed: {exc}. Report: {output}")
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Access scope and API key migration operations: move API keys between access scopes, compare a\n"
            "scope's definition across environments, refresh (re-import) a scope that keys are attached to,\n"
            "and export a scope to a .eas file. If the first argument is not a subcommand, 'migrate' is assumed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.access_scope validate --scope-id MyScope --reference-env SourceEnv "
            "--target-env MyTestEnv\n"
            "  python -m kinetic_devops.access_scope export-eas --scope-id MyScope --source-env SourceEnv\n"
            "  python -m kinetic_devops.access_scope <command> -h"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    migrate = subparsers.add_parser(
        "migrate",
        help="Migrate API key scope assignments",
        description=(
            "WRITES TO EPICOR (target environment): finds the API keys assigned to --from-scope in the source\n"
            "environment, matches them to target keys (by KeyID, else by normalized KeyID/Name), and assigns\n"
            "the matched target keys to --to-scope, then verifies each assignment. Stops without changes on\n"
            "ambiguous matches, or on missing target keys unless --allow-missing-target-keys. A JSON report is\n"
            "always written. Strategies:\n"
            "  direct         assign --to-scope directly (it must already exist in the target)\n"
            "  detach-rebind  clear the keys' scope first, optionally pause / run --import-command so the scope\n"
            "                 can be re-imported, then assign --to-scope"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python -m kinetic_devops.access_scope migrate --source-env SourceEnv --target-env MyTestEnv "
            "--from-scope OldScope --to-scope NewScope --dry-run"
        ),
    )
    migrate.add_argument("--source-env", metavar="ENV", required=True, help="Source environment nickname (read-only side)")
    migrate.add_argument("--source-user", metavar="USER_ID", default="", help="Optional source user override")
    migrate.add_argument("--source-company", metavar="COMPANY", default="", help="Optional source company override")
    migrate.add_argument("--target-env", metavar="ENV", required=True, help="Target environment nickname (keys are updated here)")
    migrate.add_argument("--target-user", metavar="USER_ID", default="", help="Optional target user override")
    migrate.add_argument("--target-company", metavar="COMPANY", default="", help="Optional target company override")
    migrate.add_argument("--from-scope", metavar="SCOPE_ID", required=True, help="Access scope ID to migrate from (in the source)")
    migrate.add_argument("--to-scope", metavar="SCOPE_ID", required=True, help="Access scope ID to assign in target")
    migrate.add_argument(
        "--strategy",
        choices=["direct", "detach-rebind"],
        default="direct",
        help="direct or detach-rebind (default: direct). Use detach-rebind only for export/import refresh flows "
             "when the scope is already in use.",
    )
    migrate.add_argument(
        "--pause-for-import",
        action="store_true",
        help="detach-rebind only: Pause after detach so you can run a fresh export/import before rebind.",
    )
    migrate.add_argument(
        "--import-command",
        metavar="CMD",
        default="",
        help="detach-rebind only: Optional shell command to execute between detach and rebind during "
             "export/import refresh (run through the system shell).",
    )
    migrate.add_argument("--allow-missing-target-keys", action="store_true", help="Continue even when some source keys are absent in target")
    migrate.add_argument(
        "--dry-run", action="store_true",
        help="Show planned actions without applying updates (the report is still written)",
    )
    migrate.add_argument(
        "--report", metavar="PATH", default="",
        help="Optional path for migration report JSON (default: temp/access_scope_migration_report_<timestamp>.json)",
    )
    KineticBaseClient.add_file_resolution_args(migrate)

    validate = subparsers.add_parser(
        "validate",
        help="Validate imported scope matches reference artifact",
        description=(
            "Read-only: builds a functional snapshot of --scope-id in the target environment (scope core, "
            "entities and BO methods; API key/user assignments are ignored) and compares it with a reference "
            "taken from --reference-artifact or --reference-env. Writes a JSON report and exits 1 on any "
            "difference unless --allow-drift. One of --reference-artifact / --reference-env is required."
        ),
    )
    validate.add_argument("--scope-id", metavar="SCOPE_ID", required=True, help="Access scope ID to validate")
    validate.add_argument(
        "--reference-artifact", metavar="JSON", default="",
        help="Reference artifact JSON with scope definition payload",
    )
    validate.add_argument(
        "--reference-env", metavar="ENV", default="",
        help="Reference environment nickname when not using --reference-artifact",
    )
    validate.add_argument("--reference-user", metavar="USER_ID", default="", help="Optional reference user override")
    validate.add_argument("--reference-company", metavar="COMPANY", default="", help="Optional reference company override")
    validate.add_argument("--target-env", metavar="ENV", required=True, help="Target environment nickname that should match reference")
    validate.add_argument("--target-user", metavar="USER_ID", default="", help="Optional target user override")
    validate.add_argument("--target-company", metavar="COMPANY", default="", help="Optional target company override")
    validate.add_argument(
        "--export-reference", metavar="PATH", default="",
        help="Optional path to save reference artifact JSON (with --reference-env; overwrites an existing file)",
    )
    validate.add_argument("--allow-drift", action="store_true", help="Exit success even when differences are found")
    validate.add_argument(
        "--report", metavar="PATH", default="",
        help="Optional path for validation report JSON "
             "(default: exports/System-Apps/AccessScopeMigr/scope_validation_report_<timestamp>.json)",
    )
    KineticBaseClient.add_file_resolution_args(validate)

    refresh_import = subparsers.add_parser(
        "refresh-import",
        help="Detach keys, run supported import, reattach keys, and validate",
        description=(
            "WRITES TO EPICOR (target environment): re-imports an access scope that API keys are attached to.\n"
            "Steps: detach every API key from --scope-id, run ONE import step (--import-eas upload+import,\n"
            "else --import-command, else --pause-for-import; one is required), check the scope exists,\n"
            "reattach the keys and verify them, and compare the result with the reference (as in 'validate').\n"
            "If a step fails, detached keys are restored to their original scope. A JSON report is written.\n"
            "One of --reference-artifact / --reference-env is required."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python -m kinetic_devops.access_scope refresh-import --scope-id MyScope --target-env MyTestEnv "
            "--reference-artifact ref.json --import-eas AccessScope_MyScope.eas --override-existing-scope --dry-run"
        ),
    )
    refresh_import.add_argument("--scope-id", metavar="SCOPE_ID", required=True, help="Access scope ID to refresh in target")
    refresh_import.add_argument(
        "--reference-artifact", metavar="JSON", default="",
        help="Reference artifact JSON with scope definition payload",
    )
    refresh_import.add_argument(
        "--reference-env", metavar="ENV", default="",
        help="Reference environment nickname when not using --reference-artifact",
    )
    refresh_import.add_argument("--reference-user", metavar="USER_ID", default="", help="Optional reference user override")
    refresh_import.add_argument("--reference-company", metavar="COMPANY", default="", help="Optional reference company override")
    refresh_import.add_argument("--target-env", metavar="ENV", required=True, help="Target environment nickname to refresh")
    refresh_import.add_argument("--target-user", metavar="USER_ID", default="", help="Optional target user override")
    refresh_import.add_argument("--target-company", metavar="COMPANY", default="", help="Optional target company override")
    refresh_import.add_argument(
        "--pause-for-import", action="store_true",
        help="Pause after detach so you can run the supported scope import manually",
    )
    refresh_import.add_argument(
        "--import-eas", metavar="FILE", default="",
        help="Path to a .eas access-scope file to upload and import natively",
    )
    refresh_import.add_argument(
        "--override-existing-scope", action="store_true",
        help="Overwrite existing scope during native file import (otherwise an existing scope blocks the import)",
    )
    refresh_import.add_argument(
        "--new-access-scope-id", metavar="SCOPE_ID", default="",
        help="Scope ID value passed to native import API (defaults to --scope-id)",
    )
    refresh_import.add_argument(
        "--import-command", metavar="CMD", default="",
        help="Optional shell command to run the supported scope import before rebind (run through the system shell)",
    )
    refresh_import.add_argument("--allow-drift", action="store_true", help="Exit success even when post-import differences remain")
    refresh_import.add_argument(
        "--dry-run", action="store_true",
        help="Show planned detach/import/reattach actions without applying updates",
    )
    refresh_import.add_argument(
        "--report", metavar="PATH", default="",
        help="Optional path for refresh/import report JSON "
             "(default: exports/System-Apps/AccessScopeMigr/scope_refresh_import_report_<timestamp>.json)",
    )
    KineticBaseClient.add_file_resolution_args(refresh_import)

    export_eas = subparsers.add_parser(
        "export-eas",
        help="Export an access scope to a local .eas file",
        description=(
            "Read-only on the server: exports --scope-id from the source environment and writes the .eas "
            "file locally. This is the only access_scope command whose output path honours --file-conflict."
        ),
    )
    export_eas.add_argument("--scope-id", metavar="SCOPE_ID", required=True, help="Access scope ID to export")
    export_eas.add_argument("--source-env", metavar="ENV", required=True, help="Environment nickname to export from")
    export_eas.add_argument("--source-user", metavar="USER_ID", default="", help="Optional source user override")
    export_eas.add_argument("--source-company", metavar="COMPANY", default="", help="Optional source company override")
    export_eas.add_argument(
        "--output", metavar="PATH", default="",
        help="Optional output .eas path (default: exports/System-Apps/AccessScopeMigr/AccessScope_<SCOPE_ID>.eas)",
    )
    KineticBaseClient.add_file_resolution_args(export_eas)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_args = list(argv if argv is not None else sys.argv[1:])
    parser = _build_parser()
    known_commands = {"migrate", "validate", "refresh-import", "export-eas"}

    if not raw_args or (raw_args[0] not in known_commands and raw_args[0] not in {"-h", "--help"}):
        raw_args = ["migrate", *raw_args]

    args = parser.parse_args(raw_args)

    if args.command == "validate":
        if not str(args.reference_artifact or "").strip() and not str(args.reference_env or "").strip():
            parser.error("validate requires either --reference-artifact or --reference-env")
        return run_scope_validation(args)

    if args.command == "refresh-import":
        if not str(args.reference_artifact or "").strip() and not str(args.reference_env or "").strip():
            parser.error("refresh-import requires either --reference-artifact or --reference-env")
        return run_scope_refresh_import(args)

    if args.command == "export-eas":
        return run_scope_export_eas(args)

    if args.command in ("migrate", None):
        return run_scope_migration(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
