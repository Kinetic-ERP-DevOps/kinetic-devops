"""
kinetic_devops/solutions.py

Solution Workbench backup and recreation support using Ice.BO.ExportPackageSvc.

Goal:
- Back up a solution definition from one environment
- Recreate the solution definition in another environment
- Build it there later, without installing a package artifact
"""

import argparse
import base64
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

if __package__:
    from .base_client import KineticBaseClient
    from .metafx import KineticMetafetcher
    from .zdatatable import KineticZDataTableService
else:
    from kinetic_devops.base_client import KineticBaseClient
    from kinetic_devops.metafx import KineticMetafetcher
    from kinetic_devops.zdatatable import KineticZDataTableService


SKIP_RECREATE_TABLES = {
    "EPHistory",
    "EPSolutionDetailDisplay",
    "EPSolutionTracker",
    "EPSolutionTrackerDetail",
}

# Default targeted hydration scope for high-value migration elements.
DEFAULT_HYDRATE_TABLES = [
    "BPDirective",
    "BPDirectiveGroup",
    "MethodDirective",
    "DataDirective",
    "Menu",
    "XXXDef",
    "MetaUI",
]

BUILD_SERVER_ARTIFACT_FIELDS = [
    ("solutionFileNameServer", "solutionFileName", "package"),
    ("solutionBuildLogFileNameServer", "solutionBuildLogFileName", "build_log"),
    ("solutionHashFileNameServer", "solutionHashFileName", "hash"),
]

FAIL_PATTERNS = [
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"\bfatal\b", re.IGNORECASE),
]

WARN_PATTERNS = [
    re.compile(r"\bwarning\b", re.IGNORECASE),
]

INSTALL_FAIL_PATTERNS = [
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
]

INSTALL_WARN_PATTERNS = [
    re.compile(r"\bwarning\b", re.IGNORECASE),
    re.compile(r"\bcancel(ed)?\b", re.IGNORECASE),
    re.compile(r"\balready exists\b", re.IGNORECASE),
    re.compile(r"\bregenerate data model\b", re.IGNORECASE),
    re.compile(r"\bmay need\b", re.IGNORECASE),
]

LAYER_EXISTS_CONFLICT_PATTERN = re.compile(
    r"Layer\s+with\s+identifier\s+([^~\s]+)~([^~\s]+)~([^~\s]+)\s+already\s+exists\s+in\s+another\s+company",
    re.IGNORECASE,
)
LAYER_EXISTS_ALT_CONFLICT_PATTERN = re.compile(
    r"application\s+layer\s+(.+?)\s+([^\s]+)\s+as\s+the\s+layer\s+already\s+exists\s+in\s+another\s+company",
    re.IGNORECASE,
)

MESSAGE_KEYS = {"message", "validationmsg", "logrecords"}

IGNORE_LINE_PATTERNS = [
    re.compile(r"\b0\s+errors?\b", re.IGNORECASE),
]


def _export_package_url(base_url: str, company: str, method_name: str) -> str:
    return f"{base_url.rstrip('/')}/api/v2/odata/{company}/Ice.BO.ExportPackageSvc/{method_name}"


def _replace_solution_ids(value: Any, source_id: str, target_id: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_solution_ids(item, source_id, target_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_solution_ids(item, source_id, target_id) for item in value]
    if isinstance(value, str):
        src = str(source_id or "").strip()
        if src and value.strip().lower() == src.lower():
            return target_id
    return value


def _sanitize_row(row: Dict[str, Any], source_id: str, target_id: str, row_mod: str = "A") -> Dict[str, Any]:
    cleaned = {
        key: value
        for key, value in row.items()
        if key not in {"SysRowID", "SysRevID", "BitFlag"}
    }
    cleaned = _replace_solution_ids(cleaned, source_id, target_id)
    cleaned["RowMod"] = row_mod
    return cleaned


def _sanitize_tableset_for_recreate(
    source_tableset: Dict[str, Any],
    source_solution_id: str,
    target_solution_id: str,
) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}

    for table_name, rows in source_tableset.items():
        if table_name in SKIP_RECREATE_TABLES:
            sanitized[table_name] = []
            continue
        if not isinstance(rows, list):
            continue

        if table_name == "ExportPackage":
            sanitized[table_name] = [
                _sanitize_row(row, source_solution_id, target_solution_id, row_mod="A")
                for row in rows[:1]
            ]
            continue

        sanitized[table_name] = [
            _sanitize_row(row, source_solution_id, target_solution_id, row_mod="A")
            for row in rows
        ]

    return sanitized


def _extract_solution_table_names(tableset: Dict[str, Any]) -> List[str]:
    table_names: Set[str] = set()
    for detail_table in ("EPSolutionDetail", "EPSolutionPackage"):
        for row in tableset.get(detail_table, []) or []:
            table_name = str(row.get("TableName", "")).strip()
            if not table_name or table_name.lower() == "undefined":
                continue
            table_names.add(table_name)
    return sorted(table_names)


def _count_solution_membership_rows(tableset: Dict[str, Any]) -> Dict[str, int]:
    detail_rows = tableset.get("EPSolutionDetail") or []
    package_rows = tableset.get("EPSolutionPackage") or []
    return {
        "detail": len(detail_rows) if isinstance(detail_rows, list) else 0,
        "package": len(package_rows) if isinstance(package_rows, list) else 0,
    }


def _as_clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _solution_registration_signature(tableset: Dict[str, Any]) -> Dict[str, List[str]]:
    detail_rows = tableset.get("EPSolutionDetail") or []
    package_rows = tableset.get("EPSolutionPackage") or []

    detail_sig: List[str] = []
    for row in detail_rows if isinstance(detail_rows, list) else []:
        if not isinstance(row, dict):
            continue
        detail_sig.append(
            "|".join(
                [
                    _as_clean_text(row.get("TableName")),
                    _as_clean_text(row.get("SolutionTypeID")),
                    _as_clean_text(row.get("Selected")),
                    _as_clean_text(row.get("Key1")),
                ]
            )
        )

    package_sig: List[str] = []
    for row in package_rows if isinstance(package_rows, list) else []:
        if not isinstance(row, dict):
            continue
        package_sig.append(
            "|".join(
                [
                    _as_clean_text(row.get("TableName")),
                    _as_clean_text(row.get("Key1")),
                ]
            )
        )

    return {
        "detail": sorted(detail_sig),
        "package": sorted(package_sig),
    }


def _diff_signatures(source_sig: List[str], target_sig: List[str]) -> Dict[str, List[str]]:
    source_only = sorted(set(source_sig) - set(target_sig))
    target_only = sorted(set(target_sig) - set(source_sig))
    return {
        "missing_in_target": source_only,
        "extra_in_target": target_only,
    }


def _normalize_table_name(name: str) -> str:
    clean = str(name or "").strip()
    if clean.lower().startswith("ice."):
        clean = clean[4:]
    return clean.lower()


def _resolve_hydrate_tables(available_table_names: List[str], requested_tables: List[str]) -> List[str]:
    requested_normalized = {_normalize_table_name(item) for item in requested_tables if str(item).strip()}
    resolved: List[str] = []
    for table_name in available_table_names:
        if _normalize_table_name(table_name) in requested_normalized:
            resolved.append(table_name)
    return sorted(set(resolved))


def _extract_dynamic_rows_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload.get("returnObj") if isinstance(payload.get("returnObj"), dict) else payload
    return {}


def _first_list_from_payload(payload: Any) -> List[Dict[str, Any]]:
    ds = _extract_dynamic_rows_payload(payload)
    if not isinstance(ds, dict):
        return []
    for value in ds.values():
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _sanitize_dynamic_payload_for_add(
    payload: Dict[str, Any],
    source_solution_id: str,
    target_solution_id: str,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for table_name, rows in payload.items():
        if not isinstance(rows, list):
            continue
        result[table_name] = [
            _sanitize_row(row, source_solution_id, target_solution_id, row_mod="A")
            for row in rows
        ]
    return result


def _line_has_pattern(line: str, patterns: List[re.Pattern]) -> bool:
    return any(pattern.search(line) for pattern in patterns)


def _collect_text_findings(path: str) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    if not path or not os.path.isfile(path):
        return findings

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            text = line.strip()
            if not text:
                continue
            if _line_has_pattern(text, IGNORE_LINE_PATTERNS):
                continue
            if _line_has_pattern(text, FAIL_PATTERNS):
                findings.append({"severity": "error", "file": path, "line": idx, "text": text})
                continue
            if _line_has_pattern(text, WARN_PATTERNS):
                findings.append({"severity": "warning", "file": path, "line": idx, "text": text})
    return findings


def _extract_named_messages(payload: Any) -> List[str]:
    messages: List[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).strip().lower() in MESSAGE_KEYS:
                    if isinstance(item, str) and item.strip():
                        messages.append(item.strip())
                    elif isinstance(item, (list, dict)):
                        _walk(item)
                else:
                    _walk(item)
            return

        if isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(payload)

    # Stable de-dup preserving first appearance order.
    deduped: List[str] = []
    seen = set()
    for message in messages:
        if message in seen:
            continue
        seen.add(message)
        deduped.append(message)
    return deduped


def _classify_install_message(message: str) -> str:
    text = str(message or "")
    if _line_has_pattern(text, INSTALL_FAIL_PATTERNS):
        return "failure"
    if _line_has_pattern(text, INSTALL_WARN_PATTERNS):
        return "warning"
    return "info"


def _build_high_vis_summary(payload: Any) -> Dict[str, Any]:
    extracted = _extract_named_messages(payload)
    classified = [
        {"severity": _classify_install_message(msg), "message": msg}
        for msg in extracted
    ]
    warnings = [item for item in classified if item["severity"] == "warning"]
    failures = [item for item in classified if item["severity"] == "failure"]
    info = [item for item in classified if item["severity"] == "info"]
    return {
        "count": len(classified),
        "failures": failures,
        "warnings": warnings,
        "info": info,
        "messages": classified,
    }


def _extract_layer_conflicts(messages: List[str]) -> List[Dict[str, str]]:
    conflicts: List[Dict[str, str]] = []
    seen = set()
    for message in messages:
        text = str(message or "").strip()
        if not text:
            continue
        match = LAYER_EXISTS_CONFLICT_PATTERN.search(text)
        if match:
            application_id = str(match.group(1) or "").strip()
            layer_name = str(match.group(2) or "").strip()
            layer_type = str(match.group(3) or "").strip()
        else:
            alt = LAYER_EXISTS_ALT_CONFLICT_PATTERN.search(text)
            if not alt:
                continue
            layer_name = str(alt.group(1) or "").strip()
            application_id = str(alt.group(2) or "").strip()
            # KNTC customization conflict in this flow is always KNTCCustLayer.
            layer_type = "KNTCCustLayer"
        key = (application_id, layer_name, layer_type)
        if key in seen:
            continue
        seen.add(key)
        conflicts.append(
            {
                "application_id": application_id,
                "layer_name": layer_name,
                "layer_type": layer_type,
                "message": text,
            }
        )
    return conflicts


def _apply_install_flags(
    settings: Dict[str, Any],
    replace: bool = False,
    overwrite_duplicate_file: bool = False,
    overwrite_duplicate_data: bool = False,
    delete_previous_install: bool = False,
    override_directives: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, bool]]:
    rows = settings.get("MainInstallSettings") or []

    # Replace mode is a convenience profile for common promotion behavior.
    eff_file = overwrite_duplicate_file or replace
    eff_data = overwrite_duplicate_data or replace
    eff_delete_prev = delete_previous_install or replace
    eff_override_dir = override_directives or replace

    for row in rows:
        row["AutoOverwriteDuplicateFile"] = bool(eff_file)
        row["AutoOverwriteDuplicateData"] = bool(eff_data)
        row["DeletePreviousInstall"] = bool(eff_delete_prev)
        row["OverrideDirectives"] = bool(eff_override_dir)

    return settings, {
        "replace": bool(replace),
        "AutoOverwriteDuplicateFile": bool(eff_file),
        "AutoOverwriteDuplicateData": bool(eff_data),
        "DeletePreviousInstall": bool(eff_delete_prev),
        "OverrideDirectives": bool(eff_override_dir),
    }


class KineticSolutionService(KineticBaseClient):
    """Backup and recreate solutions from Solution Workbench."""

    def _call(self, method_name: str, body: Optional[Dict[str, Any]] = None, company: str = "") -> Dict[str, Any]:
        target_co = company if company else self.config["company"]
        url = _export_package_url(self.config["url"], target_co, method_name)
        return self.execute_request("POST", url, payload=body or {}, company=target_co)

    def _call_file_transfer(self, method_name: str, body: Optional[Dict[str, Any]] = None, company: str = "") -> Dict[str, Any]:
        target_co = company if company else self.config["company"]
        url = f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/Ice.Lib.FileTransferSvc/{method_name}"
        return self.execute_request("POST", url, payload=body or {}, company=target_co)

    def _get_metafx_service(self) -> KineticMetafetcher:
        svc = getattr(self, "_metafx_service", None)
        if svc is None:
            svc = KineticMetafetcher(
                env_nickname=self.config.get("nickname") or None,
                user_id=self.config.get("user_id") or None,
                company_id=self.config.get("company") or None,
            )
            self._metafx_service = svc
        return svc

    def _get_zdatatable_service(self) -> KineticZDataTableService:
        svc = getattr(self, "_zdatatable_service", None)
        if svc is None:
            svc = KineticZDataTableService(
                env_nickname=self.config.get("nickname") or None,
                user_id=self.config.get("user_id") or None,
                company_id=self.config.get("company") or None,
            )
            self._zdatatable_service = svc
        return svc

    def _delete_layer_conflict(
        self,
        application_id: str,
        layer_name: str,
        layer_type: str,
        company: str = "",
    ) -> Dict[str, Any]:
        current_company = str(company or self.config.get("company") or "").strip()
        metafx_service = self._get_metafx_service()
        scan_rows = metafx_service.get_layers(
            view_id=application_id,
            type_code=layer_type,
            device_type="Desktop",
            include_unpublished_layers=True,
            company=company,
        )

        matching = [
            row
            for row in scan_rows
            if str(row.get("ViewId") or "").strip() == application_id
            and str(row.get("LayerName") or "").strip() == layer_name
            and str(row.get("TypeCode") or "").strip() == layer_type
        ]

        preferred = [
            row
            for row in matching
            if str(row.get("Company") or "").strip().startswith("T_")
            or str(row.get("Company") or "").strip() != current_company
        ]
        to_delete = preferred or []
        if not to_delete:
            return {
                "deleted": False,
                "reason": "No tenant or cross-company matching layer found",
                "application_id": application_id,
                "layer_name": layer_name,
                "layer_type": layer_type,
                "matches": matching,
            }

        delete_results: List[Dict[str, Any]] = []
        for row in to_delete:
            try:
                delete_resp = metafx_service.delete_layer(row, company=company)
                delete_results.append(
                    {
                        "success": True,
                        "row": row,
                        "response": delete_resp,
                        "method": "DeleteLayer",
                    }
                )
            except Exception as exc:
                # Some tenants only allow bulk delete for specific layer types.
                try:
                    bulk_resp = metafx_service.bulk_delete_layers([row], company=company)
                    delete_results.append(
                        {
                            "success": True,
                            "row": row,
                            "response": bulk_resp,
                            "method": "BulkDeleteLayers",
                        }
                    )
                except Exception as inner_exc:
                    delete_results.append(
                        {
                            "success": False,
                            "row": row,
                            "method": "DeleteLayer",
                            "error": str(exc),
                            "fallback_method": "BulkDeleteLayers",
                            "fallback_error": str(inner_exc),
                        }
                    )

        deleted_any = any(bool(item.get("success")) for item in delete_results)
        return {
            "deleted": bool(deleted_any),
            "application_id": application_id,
            "layer_name": layer_name,
            "layer_type": layer_type,
            "matches": matching,
            "deleted_rows": to_delete,
            "delete_results": delete_results,
        }

    def _empty_tableset_from(self, tableset: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not tableset:
            return {"ExportPackage": []}
        return {key: [] for key, value in tableset.items() if isinstance(value, list)}

    def list_solutions(self, company: str = "", page_size: int = 200, absolute_page: int = 1) -> Dict[str, Any]:
        return self._call(
            "GetList",
            {
                "whereClause": "",
                "pageSize": page_size,
                "absolutePage": absolute_page,
            },
            company=company,
        )

    def get_solution(self, solution_id: str, company: str = "") -> Dict[str, Any]:
        return self._call("GetByID", {"packageID": solution_id}, company=company)

    def solution_exists(self, solution_id: str, company: str = "") -> bool:
        try:
            data = self.get_solution(solution_id, company=company)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (400, 404):
                return False
            raise
        tableset = data.get("returnObj") or {}
        for key in ("ExportPackage", "EPSolutionHeader", "EPSolutionDetail", "EPSolutionPackage"):
            rows = tableset.get(key) or []
            if rows:
                return True
        return False

    def get_solution_items_dynamic(self, solution_id: str, table_name: str, company: str = "") -> Dict[str, Any]:
        return self._call(
            "GetSolutionItemsAsDynamicDataSet",
            {"tableName": table_name, "solutionId": solution_id},
            company=company,
        )

    def get_tracked_items_dynamic(self, solution_id: str, table_name: str, company: str = "") -> Dict[str, Any]:
        return self._call(
            "GetTrackedItemsAsDynamicDataSet",
            {"tableName": table_name, "solutionId": solution_id},
            company=company,
        )

    def get_new_export_package(self, template_tableset: Optional[Dict[str, Any]] = None, company: str = "") -> Dict[str, Any]:
        empty_ds = self._empty_tableset_from(template_tableset)
        response = self._call("GetNewExportPackage", {"ds": empty_ds}, company=company)
        return (response.get("parameters") or {}).get("ds") or empty_ds

    def update_solution_tableset(self, tableset: Dict[str, Any], company: str = "") -> Dict[str, Any]:
        response = self._call("Update", {"ds": tableset}, company=company)
        return (response.get("parameters") or {}).get("ds") or {}

    def delete_solution(self, solution_id: str, company: str = "") -> None:
        self._call("DeleteByID", {"solutionID": solution_id}, company=company)

    def remove_solution(self, solution_id: str, company: str = "", require_exists: bool = False) -> Dict[str, Any]:
        exists_before = self.solution_exists(solution_id, company=company)
        if not exists_before:
            if require_exists:
                raise ValueError(f"Solution does not exist in target environment: {solution_id}")
            return {
                "success": True,
                "solution_id": solution_id,
                "company": company or self.config.get("company", ""),
                "exists_before": False,
                "exists_after": False,
                "removed_membership": {"detail": 0, "package": 0},
                "note": "No-op: solution did not exist.",
            }

        current = self.get_solution(solution_id, company=company).get("returnObj") or {}
        before_membership = _count_solution_membership_rows(current)

        self.delete_solution(solution_id, company=company)

        exists_after = self.solution_exists(solution_id, company=company)
        if exists_after:
            raise RuntimeError(f"Solution removal failed; solution still exists: {solution_id}")

        return {
            "success": True,
            "solution_id": solution_id,
            "company": company or self.config.get("company", ""),
            "exists_before": True,
            "exists_after": False,
            "removed_membership": before_membership,
            "note": "Removed Solution Workbench registration rows (definition/membership). Underlying artifacts are not globally deleted by this operation.",
        }

    def add_items_to_solution_and_save(
        self,
        solution_id: str,
        ds_to_add: Dict[str, Any],
        records_to_delete: Optional[Dict[str, Any]] = None,
        company: str = "",
    ) -> Dict[str, Any]:
        return self._call(
            "AddItemsToSolutionAndSave",
            {
                "solutionId": solution_id,
                "dsToAdd": ds_to_add,
                "recordsToDelete": records_to_delete if records_to_delete is not None else {"ELEMENT": []},
            },
            company=company,
        )

    def _do_element_search(
        self,
        element_header_id: str,
        parent_table_name: str,
        bo_method: str,
        company: str = "",
    ) -> List[Dict[str, Any]]:
        response = self._call(
            "DoElementSearch",
            {
                "elementHeaderId": element_header_id,
                "parentTableName": parent_table_name,
                "boMethod": bo_method,
            },
            company=company,
        )
        return _first_list_from_payload(response)

    def _zdatatable_exists(self, system_code: str, table_id: str, company: str = "") -> bool:
        return self._get_zdatatable_service().exists_ud_table(system_code, table_id, company=company)

    def _build_selected_solution_rows_from_dynamic(
        self,
        source_dynamic: Dict[str, Any],
        company: str = "",
    ) -> List[Dict[str, Any]]:
        src_bp = _first_list_from_payload(source_dynamic.get("BpDirective") or {})
        src_menu = _first_list_from_payload(source_dynamic.get("Menu") or {})
        src_meta = _first_list_from_payload(source_dynamic.get("MetaUI") or {})
        src_zdt = _first_list_from_payload(source_dynamic.get("ZDataTable") or {})

        pilot_bp = self._do_element_search("BpDirective", "BpDirective", "", company=company)
        pilot_menu = self._do_element_search("Menu", "Menu", "", company=company)
        pilot_meta = self._do_element_search("KNTCCustLayer", "MetaUI", "KNTCCustLayer", company=company)
        pilot_zdt = self._do_element_search("ZDataTable", "ZDataTable", "", company=company)

        selected: List[Dict[str, Any]] = []

        bp_index = {
            (str(row.get("Name") or ""), str(row.get("BpMethodCode") or ""), str(row.get("DirectiveType") or "")): row
            for row in pilot_bp
        }
        for row in src_bp:
            key = (str(row.get("Name") or ""), str(row.get("BpMethodCode") or ""), str(row.get("DirectiveType") or ""))
            match = bp_index.get(key)
            sys_row_id = str(match.get("SysRowID") or "").strip() if isinstance(match, dict) else ""
            if not sys_row_id:
                continue
            selected.append(
                {
                    "SysRowID": sys_row_id,
                    "ApplicationId": None,
                    "KeyOrSysRowID": sys_row_id,
                    "TableName": "BpDirective",
                    "ElementType": "Data",
                }
            )

        menu_index = {str(row.get("MenuID") or ""): row for row in pilot_menu}
        for row in src_menu:
            key = str(row.get("MenuID") or "")
            match = menu_index.get(key)
            sys_row_id = str(match.get("SysRowID") or "").strip() if isinstance(match, dict) else ""
            if not sys_row_id:
                continue
            selected.append(
                {
                    "SysRowID": sys_row_id,
                    "ApplicationId": None,
                    "KeyOrSysRowID": sys_row_id,
                    "TableName": "Menu",
                    "ElementType": "Data",
                }
            )

        meta_index = {
            (str(row.get("ApplicationId") or ""), str(row.get("Type") or ""), str(row.get("LayerName") or "")): row
            for row in pilot_meta
        }
        for row in src_meta:
            app_id = str(row.get("ApplicationId") or "")
            layer_name = str(row.get("LayerName") or "")
            layer_type = str(row.get("Type") or "")
            key = (app_id, layer_type, layer_name)
            if key not in meta_index:
                continue
            composite = f"{app_id}~{layer_name}~{layer_type}"
            selected.append(
                {
                    "SysRowID": None,
                    "ApplicationId": composite,
                    "KeyOrSysRowID": composite,
                    "TableName": "MetaUI",
                    "ElementType": "Data",
                }
            )

        zdt_index = {str(row.get("DataTableID") or ""): row for row in pilot_zdt}
        for row in src_zdt:
            key = str(row.get("DataTableID") or "")
            system_code = str(row.get("SystemCode") or "Erp").strip() or "Erp"
            if key and not self._zdatatable_exists(system_code, key, company=company):
                continue
            match = zdt_index.get(key)
            sys_row_id = str(match.get("SysRowID") or "").strip() if isinstance(match, dict) else ""
            if not sys_row_id:
                continue
            selected.append(
                {
                    "SysRowID": sys_row_id,
                    "ApplicationId": None,
                    "KeyOrSysRowID": sys_row_id,
                    "TableName": "ZDataTable",
                    "ElementType": "Data",
                }
            )

        deduped: List[Dict[str, Any]] = []
        seen = set()
        for row in selected:
            key = (str(row.get("TableName") or ""), str(row.get("KeyOrSysRowID") or ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        return deduped

    def get_build_settings(self, solution_id: str, company: str = "") -> Dict[str, Any]:
        response = self._call("GetBuildSettings", {"solutionID": solution_id}, company=company)
        return response.get("returnObj") or {}

    def build_solution(self, solution_id: str, company: str = "") -> Dict[str, Any]:
        settings = self.get_build_settings(solution_id, company=company)
        response = self._call("BuildSolution", {"settings": settings}, company=company)
        result = {
            "solution_id": solution_id,
            "settings": settings,
            "result": response,
        }
        return result

    def download_server_file(self, server_path: str, folder: int = 4, company: str = "") -> bytes:
        response = self._call_file_transfer(
            "DownloadFile",
            {"folder": int(folder), "serverPath": server_path},
            company=company,
        )
        b64_data = response.get("returnObj")
        if not isinstance(b64_data, str) or not b64_data:
            raise ValueError(f"No file data returned for server path: {server_path}")
        return base64.b64decode(b64_data)

    def validate_build_artifacts(
        self,
        package_file: str,
        build_log_file: str = "",
        validation_file: str = "",
    ) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "package_file": package_file,
            "package_exists": False,
            "package_is_zip": False,
            "package_entries": 0,
            "errors": [],
            "warnings": [],
            "pre_install_ready": False,
        }

        if not package_file or not os.path.isfile(package_file):
            summary["errors"].append("Package file is missing")
            return summary

        summary["package_exists"] = True
        if not zipfile.is_zipfile(package_file):
            summary["errors"].append("Package is not a valid zip file")
            return summary

        summary["package_is_zip"] = True
        with zipfile.ZipFile(package_file, "r") as zf:
            names = zf.namelist()
            summary["package_entries"] = len(names)
            if not names:
                summary["errors"].append("Package zip contains no entries")

        findings: List[Dict[str, Any]] = []
        findings.extend(_collect_text_findings(build_log_file))
        findings.extend(_collect_text_findings(validation_file))

        summary["warnings"] = [f for f in findings if f.get("severity") == "warning"]
        summary["errors"].extend([f for f in findings if f.get("severity") == "error"])
        summary["pre_install_ready"] = bool(summary["package_is_zip"] and not summary["errors"])
        return summary

    def build_and_download(
        self,
        solution_id: str,
        out_dir: str,
        company: str = "",
        folder: int = 4,
    ) -> Dict[str, Any]:
        build_result = self.build_solution(solution_id, company=company)
        params = ((build_result.get("result") or {}).get("parameters") or {})
        os.makedirs(out_dir, exist_ok=True)

        downloaded_artifacts: List[Dict[str, Any]] = []
        server_dirs: List[str] = []
        for server_key, local_key, role in BUILD_SERVER_ARTIFACT_FIELDS:
            server_path = str(params.get(server_key) or "").strip()
            if not server_path:
                continue

            server_dir = os.path.dirname(server_path)
            if server_dir and server_dir not in server_dirs:
                server_dirs.append(server_dir)

            payload = self.download_server_file(server_path, folder=folder, company=company)
            local_name = str(params.get(local_key) or "").strip()
            output_name = os.path.basename(local_name or server_path)
            if not output_name:
                output_name = f"{solution_id}_{role}.bin"

            output_path = os.path.join(out_dir, output_name)
            output_path = self.resolve_output_path(output_path, conflict_resolution="timestamp")
            with open(output_path, "wb") as f:
                f.write(payload)

            downloaded_artifacts.append(
                {
                    "role": role,
                    "server_field": server_key,
                    "server_path": server_path,
                    "local_file": output_path,
                }
            )

        if not downloaded_artifacts:
            raise ValueError("Build did not return downloadable server artifact file names")

        # Preserve compatibility for callers expecting single artifact keys.
        package_artifact = next((a for a in downloaded_artifacts if a["role"] == "package"), None)
        if package_artifact:
            build_result["artifact_file"] = package_artifact["local_file"]
            build_result["artifact_server_path"] = package_artifact["server_path"]

        # Try to download server-side Validation.txt from the same folder as build artifacts.
        validation_downloaded = False
        for server_dir in server_dirs:
            for validation_name in ("Validation.txt", "validation.txt"):
                normalized_server_dir = server_dir.rstrip("/\\")
                validation_server_path = f"{normalized_server_dir}/{validation_name}"
                
                try:
                    payload = self.download_server_file(validation_server_path, folder=folder, company=company)
                except Exception:
                    continue

                validation_local_path = os.path.join(out_dir, "Validation.txt")
                validation_local_path = self.resolve_output_path(validation_local_path, conflict_resolution="timestamp")
                with open(validation_local_path, "wb") as f:
                    f.write(payload)

                downloaded_artifacts.append(
                    {
                        "role": "validation_server",
                        "server_field": "derived_validation_path",
                        "server_path": validation_server_path,
                        "local_file": validation_local_path,
                    }
                )
                build_result["validation_file"] = validation_local_path
                build_result["validation_server_path"] = validation_server_path
                validation_downloaded = True
                break
            if validation_downloaded:
                break

        log_records = str(params.get("logRecords") or "").strip()
        if log_records and not validation_downloaded:
            validation_path = os.path.join(out_dir, "Validation.txt")
            validation_path = self.resolve_output_path(validation_path, conflict_resolution="timestamp")
            with open(validation_path, "w", encoding="utf-8") as f:
                f.write(log_records)
            build_result["validation_file"] = validation_path
            build_result["validation_source"] = "logRecords"
        elif validation_downloaded:
            build_result["validation_source"] = "server_file"

        build_log_file = ""
        build_log_artifact = next((a for a in downloaded_artifacts if a.get("role") == "build_log"), None)
        if build_log_artifact:
            build_log_file = str(build_log_artifact.get("local_file") or "")

        validation_file = str(build_result.get("validation_file") or "")
        package_file = str(build_result.get("artifact_file") or "")
        build_result["post_build_validation"] = self.validate_build_artifacts(
            package_file=package_file,
            build_log_file=build_log_file,
            validation_file=validation_file,
        )
        build_result["pre_install_ready"] = bool(build_result["post_build_validation"].get("pre_install_ready"))

        build_result["downloaded_artifacts"] = downloaded_artifacts
        build_result["artifact_folder"] = int(folder)
        return build_result

    def save_build_result(self, solution_id: str, build_result: Dict[str, Any], out_dir: str) -> str:
        os.makedirs(out_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_solution = solution_id.replace("/", "_").replace("\\", "_")
        output_path = os.path.join(out_dir, f"solution_build_{safe_solution}_{timestamp}.json")
        output_path = self.resolve_output_path(output_path, conflict_resolution="error")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(build_result, f, indent=2, ensure_ascii=False)
        return output_path

    def get_install_settings(self, cab_data: str, solution_file: str, company: str = "") -> Dict[str, Any]:
        response = self._call(
            "GetInstallSettings",
            {"cabData": cab_data, "solutionFile": solution_file},
            company=company,
        )
        return response.get("returnObj") or {}

    def install_solution_cab(
        self,
        cab_file: str,
        solution_file: str = "",
        company: str = "",
        skip_preflight: bool = False,
        replace: bool = False,
        overwrite_duplicate_file: bool = False,
        overwrite_duplicate_data: bool = False,
        delete_previous_install: bool = False,
        override_directives: bool = False,
        auto_clear_layer_conflicts: bool = False,
    ) -> Dict[str, Any]:
        if not os.path.isfile(cab_file):
            raise FileNotFoundError(f"CAB file not found: {cab_file}")

        if not skip_preflight:
            base = cab_file
            build_log_path = f"{base}_Build.log"
            validation_path = f"{base}_Validation.txt"
            if not os.path.isfile(validation_path):
                alt_validation = os.path.join(os.path.dirname(base), "Validation.txt")
                validation_path = alt_validation if os.path.isfile(alt_validation) else ""

            preflight = self.validate_build_artifacts(
                package_file=cab_file,
                build_log_file=build_log_path if os.path.isfile(build_log_path) else "",
                validation_file=validation_path,
            )
            if not preflight.get("pre_install_ready"):
                raise ValueError("Pre-install validation failed. Use --skip-preflight to bypass.")

        cab_data = self.file_to_base64(cab_file)
        solution_file_name = solution_file.strip() or os.path.basename(cab_file)
        settings = self.get_install_settings(cab_data, solution_file_name, company=company)
        settings, install_flags = _apply_install_flags(
            settings,
            replace=replace,
            overwrite_duplicate_file=overwrite_duplicate_file,
            overwrite_duplicate_data=overwrite_duplicate_data,
            delete_previous_install=delete_previous_install,
            override_directives=override_directives,
        )

        try:
            response = self._call(
                "Install",
                {"cabData": cab_data, "settings": settings},
                company=company,
            )
        except Exception as exc:
            # Catch error-status responses that might contain conflict details in the body
            resp_obj = getattr(exc, "response", None)
            if resp_obj is not None:
                try:
                    response = resp_obj.json()
                except Exception:
                    # If not JSON, re-raise the original error
                    raise exc
            else:
                raise exc

        high_vis = _build_high_vis_summary(response)
        conflict_actions: List[Dict[str, Any]] = []
        retried = False
        all_messages = [item.get("message", "") for item in high_vis.get("messages") or []]
        conflicts = _extract_layer_conflicts([str(item or "") for item in all_messages])

        # Default policy is safe: fail on cross-company MetaFX layer conflicts unless user explicitly forces cleanup.
        if conflicts and not auto_clear_layer_conflicts:
            raise RuntimeError(
                "Install reported MetaFX layer conflicts across companies and was stopped by default policy. "
                "Re-run with --auto-clear-layer-conflicts to force tenant/cross-company layer cleanup and retry. "
                f"Detected conflicts: {len(conflicts)}."
            )

        if auto_clear_layer_conflicts:
            for conflict in conflicts:
                try:
                    action = self._delete_layer_conflict(
                        application_id=conflict["application_id"],
                        layer_name=conflict["layer_name"],
                        layer_type=conflict["layer_type"],
                        company=company,
                    )
                    action["source_message"] = conflict.get("message", "")
                    conflict_actions.append(action)
                except Exception as exc:
                    conflict_actions.append(
                        {
                            "deleted": False,
                            "application_id": conflict.get("application_id", ""),
                            "layer_name": conflict.get("layer_name", ""),
                            "layer_type": conflict.get("layer_type", ""),
                            "source_message": conflict.get("message", ""),
                            "error": str(exc),
                        }
                    )

            if any(bool(item.get("deleted")) for item in conflict_actions):
                retried = True
                settings = self.get_install_settings(cab_data, solution_file_name, company=company)
                settings, install_flags = _apply_install_flags(
                    settings,
                    replace=replace,
                    overwrite_duplicate_file=overwrite_duplicate_file,
                    overwrite_duplicate_data=overwrite_duplicate_data,
                    delete_previous_install=delete_previous_install,
                    override_directives=override_directives,
                )
                response = self._call(
                    "Install",
                    {"cabData": cab_data, "settings": settings},
                    company=company,
                )
                high_vis = _build_high_vis_summary(response)

                retry_messages = [item.get("message", "") for item in high_vis.get("messages") or []]
                remaining_conflicts = _extract_layer_conflicts([str(item or "") for item in retry_messages])
                if remaining_conflicts:
                    raise RuntimeError(
                        "Install retry still reports MetaFX layer conflicts after forced cleanup. "
                        f"Remaining conflicts: {len(remaining_conflicts)}."
                    )
            elif conflicts:
                raise RuntimeError(
                    "Install reported MetaFX layer conflicts, but no eligible tenant/cross-company layers were deleted. "
                    "Resolve manually in Solution Workbench or MetaFX and retry install."
                )

        return {
            "cab_file": cab_file,
            "solution_file": solution_file_name,
            "preflight": preflight if not skip_preflight else {"skipped": True},
            "install_flags": install_flags,
            "settings": settings,
            "result": response,
            "high_vis": high_vis,
            "layer_conflict_auto_clear": {
                "enabled": bool(auto_clear_layer_conflicts),
                "retried": bool(retried),
                "actions": conflict_actions,
            },
        }

    def backup_solution(
        self,
        solution_id: str,
        out_dir: str,
        company: str = "",
        include_dynamic: bool = True,
        include_tracked: bool = True,
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        tableset = (self.get_solution(solution_id, company=company).get("returnObj") or {})
        table_names = _extract_solution_table_names(tableset)

        dynamic_items: Dict[str, Any] = {}
        tracked_items: Dict[str, Any] = {}

        if include_dynamic:
            for table_name in table_names:
                dynamic_items[table_name] = self.get_solution_items_dynamic(solution_id, table_name, company=company)

        if include_tracked:
            for table_name in table_names:
                tracked_items[table_name] = self.get_tracked_items_dynamic(solution_id, table_name, company=company)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_solution = solution_id.replace("/", "_").replace("\\", "_")
        output_path = os.path.join(out_dir, f"solution_backup_{safe_solution}_{timestamp}.json")
        output_path = self.resolve_output_path(output_path, conflict_resolution="error")

        backup = {
            "metadata": {
                "solution_id": solution_id,
                "environment": self.config.get("nickname", ""),
                "company": company or self.config.get("company", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "table_names": table_names,
            },
            "tableset": tableset,
            "dynamic_items": dynamic_items,
            "tracked_items": tracked_items,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(backup, f, indent=2, ensure_ascii=False)

        return {
            "success": True,
            "solution_id": solution_id,
            "output_file": output_path,
            "table_names": table_names,
        }

    def _hydrate_solution_membership_from_backup(
        self,
        source_tableset: Dict[str, Any],
        source_dynamic: Dict[str, Any],
        source_tracked: Dict[str, Any],
        metadata: Dict[str, Any],
        source_solution_id: str,
        final_solution_id: str,
        company: str = "",
        hydrate_tables: Optional[List[str]] = None,
    ) -> Tuple[List[str], Dict[str, str]]:
        hydrated_tables: List[str] = []
        hydrate_errors: Dict[str, str] = {}

        available_names = metadata.get("table_names") or _extract_solution_table_names(source_tableset)
        requested = hydrate_tables or DEFAULT_HYDRATE_TABLES
        selected_tables = _resolve_hydrate_tables(available_names, requested)

        for table_name in selected_tables:
            raw_payload = source_dynamic.get(table_name) or source_tracked.get(table_name)
            if not raw_payload:
                continue
            ds_obj = _extract_dynamic_rows_payload(raw_payload)
            if not ds_obj:
                continue
            ds_to_add = _sanitize_dynamic_payload_for_add(ds_obj, source_solution_id, final_solution_id)
            if not ds_to_add:
                continue
            try:
                self.add_items_to_solution_and_save(
                    final_solution_id,
                    ds_to_add=ds_to_add,
                    records_to_delete={"ELEMENT": []},
                    company=company,
                )
                hydrated_tables.append(table_name)
            except Exception as exc:
                hydrate_errors[table_name] = str(exc)

        # Preferred hydration path mirrors Solution Workbench UI behavior.
        try:
            selected_rows = self._build_selected_solution_rows_from_dynamic(source_dynamic, company=company)
            if selected_rows:
                populate = self._call(
                    "PopulateItemsWithDependencies",
                    {"ds": {"SelectedSolutionElementList": selected_rows}},
                    company=company,
                )
                returned_rows = populate.get("returnObj") or []
                if isinstance(returned_rows, dict):
                    ds_to_add = {"ELEMENT": returned_rows.get("ELEMENT") or []}
                elif isinstance(returned_rows, list):
                    ds_to_add = {"ELEMENT": returned_rows}
                else:
                    ds_to_add = {"ELEMENT": []}
                if ds_to_add.get("ELEMENT"):
                    self.add_items_to_solution_and_save(
                        final_solution_id,
                        ds_to_add=ds_to_add,
                        records_to_delete={"ELEMENT": []},
                        company=company,
                    )
        except Exception as exc:
            hydrate_errors["ui_rebind"] = str(exc)

        return sorted(set(hydrated_tables)), hydrate_errors

    def recreate_solution_from_backup(
        self,
        backup_path: str,
        company: str = "",
        target_solution_id: str = "",
        overwrite: bool = False,
        hydrate_items: bool = False,
        hydrate_tables: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        with open(backup_path, "r", encoding="utf-8") as f:
            backup = json.load(f)

        source_tableset = backup.get("tableset") or {}
        source_dynamic = backup.get("dynamic_items") or {}
        source_tracked = backup.get("tracked_items") or {}
        metadata = backup.get("metadata") or {}
        source_membership = _count_solution_membership_rows(source_tableset)
        source_solution_id = str(metadata.get("solution_id", "")).strip()
        if not source_solution_id:
            raise ValueError("Backup file is missing metadata.solution_id")

        final_solution_id = target_solution_id.strip() or source_solution_id

        if overwrite and self.solution_exists(final_solution_id, company=company):
            self.delete_solution(final_solution_id, company=company)
        elif self.solution_exists(final_solution_id, company=company):
            raise ValueError(f"Solution already exists in target environment: {final_solution_id}")

        base_ds = self.get_new_export_package(template_tableset=source_tableset, company=company)
        recreated_ds = _sanitize_tableset_for_recreate(source_tableset, source_solution_id, final_solution_id)

        for key, value in recreated_ds.items():
            if isinstance(value, list):
                base_ds[key] = value

        saved_ds = self.update_solution_tableset(base_ds, company=company)

        hydrated_tables: List[str] = []
        hydrate_errors: Dict[str, str] = {}
        auto_hydrate_attempted = False

        if hydrate_items:
            hydrated_tables, hydrate_errors = self._hydrate_solution_membership_from_backup(
                source_tableset=source_tableset,
                source_dynamic=source_dynamic,
                source_tracked=source_tracked,
                metadata=metadata,
                source_solution_id=source_solution_id,
                final_solution_id=final_solution_id,
                company=company,
                hydrate_tables=hydrate_tables,
            )

        target_tableset = (self.get_solution(final_solution_id, company=company).get("returnObj") or {})
        target_membership = _count_solution_membership_rows(target_tableset)
        source_signature = _solution_registration_signature(source_tableset)
        target_signature = _solution_registration_signature(target_tableset)

        source_has_members = source_membership["detail"] > 0 or source_membership["package"] > 0
        target_has_no_members = target_membership["detail"] == 0 and target_membership["package"] == 0

        # Codified fallback: attempt hydration automatically when recreate leaves membership empty.
        if source_has_members and target_has_no_members and not hydrate_items:
            auto_hydrate_attempted = True
            auto_tables, auto_errors = self._hydrate_solution_membership_from_backup(
                source_tableset=source_tableset,
                source_dynamic=source_dynamic,
                source_tracked=source_tracked,
                metadata=metadata,
                source_solution_id=source_solution_id,
                final_solution_id=final_solution_id,
                company=company,
                hydrate_tables=hydrate_tables,
            )
            hydrated_tables = sorted(set(hydrated_tables + auto_tables))
            for key, value in auto_errors.items():
                hydrate_errors[f"auto:{key}"] = value

            target_tableset = (self.get_solution(final_solution_id, company=company).get("returnObj") or {})
            target_membership = _count_solution_membership_rows(target_tableset)
            target_signature = _solution_registration_signature(target_tableset)
            target_has_no_members = target_membership["detail"] == 0 and target_membership["package"] == 0

        # Guardrail: some environments report recreate success but persist zero linked rows.
        if source_has_members and target_has_no_members:
            guidance = (
                "Automatic hydration was attempted but no linked items persisted. "
                if auto_hydrate_attempted or hydrate_items
                else ""
            )
            raise RuntimeError(
                "Recreate completed but target solution has no linked items "
                f"(detail={target_membership['detail']}, package={target_membership['package']}). "
                f"{guidance}"
                "This environment may require adding elements through Solution Workbench UI "
                "(Add To Solution) to resolve target GUID bindings before build/install."
            )

        # Validation gate: target must logically match source registration intent.
        detail_matches = source_signature["detail"] == target_signature["detail"]
        package_matches = source_signature["package"] == target_signature["package"]
        if source_has_members and (not detail_matches or not package_matches):
            detail_diff = _diff_signatures(source_signature["detail"], target_signature["detail"])
            package_diff = _diff_signatures(source_signature["package"], target_signature["package"])
            raise RuntimeError(
                "Recreate completed but target registration does not match source intent. "
                f"detail_matches={detail_matches}, package_matches={package_matches}, "
                f"detail_missing={len(detail_diff['missing_in_target'])}, detail_extra={len(detail_diff['extra_in_target'])}, "
                f"package_missing={len(package_diff['missing_in_target'])}, package_extra={len(package_diff['extra_in_target'])}."
            )

        return {
            "success": True,
            "source_solution_id": source_solution_id,
            "target_solution_id": final_solution_id,
            "company": company or self.config.get("company", ""),
            "source_membership": source_membership,
            "target_membership": target_membership,
            "tables_saved": sorted([key for key, value in base_ds.items() if isinstance(value, list) and value]),
            "hydrated_tables": sorted(hydrated_tables),
            "hydrate_errors": hydrate_errors,
            "auto_hydrate_attempted": auto_hydrate_attempted,
            "target_matches_source_signature": bool(detail_matches and package_matches),
            "saved_tableset": saved_ds,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Backup, recreate, build, and install Solution Workbench solutions (Ice.BO.SolutionWorkbenchSvc).\n"
            "Global options (--env/--user/--company and the file options) go BEFORE the subcommand.\n"
            "Results are printed as JSON."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.solutions --env MyTestEnv list\n"
            "  python -m kinetic_devops.solutions --env MyTestEnv backup MySolution\n"
            "  python -m kinetic_devops.solutions --env MyTestEnv build MySolution --out-dir exports/solutions/build"
        ),
    )
    parser.add_argument(
        "--env", metavar="ENV",
        help="Environment nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument("--user", metavar="USER_ID", help="User ID for session")
    parser.add_argument("--company", metavar="COMPANY", help="Company override (default: the session's company)", default="")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    list_parser = subparsers.add_parser(
        "list",
        help="List solutions",
        description="Read-only: prints one page of Solution Workbench solution definitions as JSON.",
    )
    list_parser.add_argument("--page-size", metavar="N", type=int, default=200, help="Rows per page (default: 200)")
    list_parser.add_argument("--page", metavar="N", type=int, default=1, help="1-based page number (default: 1)")

    backup_parser = subparsers.add_parser(
        "backup",
        help="Backup one solution definition",
        description=(
            "Read-only on the server: saves the solution's tableset plus, per element table, its dynamic "
            "items and tracked items to <out-dir>/solution_backup_<SOLUTION_ID>_<UTC timestamp>.json, "
            "for use with 'recreate'."
        ),
    )
    backup_parser.add_argument("solution_id", metavar="SOLUTION_ID", help="Solution ID to back up")
    backup_parser.add_argument(
        "--out-dir", metavar="DIR", default=os.path.join("exports", "solutions"),
        help="Output directory, created if missing (default: %(default)s)",
    )
    backup_parser.add_argument("--no-dynamic", action="store_true", help="Skip fetching the dynamic solution items")
    backup_parser.add_argument("--no-tracked", action="store_true", help="Skip fetching the tracked items")

    recreate_parser = subparsers.add_parser(
        "recreate",
        help="Recreate a solution from backup JSON",
        description=(
            "WRITES TO EPICOR: creates the solution definition saved by 'backup' in the selected environment "
            "(optionally under a new ID). Fails if the solution already exists unless --overwrite, which "
            "DELETES the existing solution definition first. With --hydrate-items it also re-adds element "
            "membership."
        ),
    )
    recreate_parser.add_argument("backup_file", metavar="BACKUP_JSON", help="Backup file written by 'backup'")
    recreate_parser.add_argument(
        "--target-solution-id", metavar="SOLUTION_ID", default="",
        help="Create under this solution ID instead of the one in the backup",
    )
    recreate_parser.add_argument(
        "--overwrite", action="store_true",
        help="DESTRUCTIVE: delete an existing solution with the same ID before recreating it",
    )
    recreate_parser.add_argument(
        "--hydrate-items",
        action="store_true",
        help="Attempt AddItemsToSolutionAndSave for targeted element tables (BPDirective, Menu, XXXDef, MetaUI)",
    )
    recreate_parser.add_argument(
        "--hydrate-table",
        metavar="TABLE",
        action="append",
        default=[],
        help="Table name to hydrate (repeatable). Defaults to BPDirective/Menu/XXXDef/MetaUI set when --hydrate-items is used.",
    )

    build_parser = subparsers.add_parser(
        "build",
        help="Build a solution package on the server",
        description=(
            "Runs BuildSolution on the server with the solution's default build settings. With --out-dir it\n"
            "also downloads the built package, build log and validation file, checks the package is a valid\n"
            "zip with no reported errors (pre_install_ready), and saves the build result JSON there."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    build_parser.add_argument("solution_id", metavar="SOLUTION_ID", help="Solution ID to build")
    build_parser.add_argument(
        "--out-dir",
        metavar="DIR",
        default="",
        help="Optional directory to download the built package and save build result JSON.",
    )
    build_parser.add_argument(
        "--file-folder",
        metavar="N",
        type=int,
        default=4,
        help="FileTransferSvc folder enum for downloading server file (default: 4).",
    )

    install_parser = subparsers.add_parser(
        "install",
        help="Install a solution CAB package",
        description=(
            "WRITES TO EPICOR: installs a solution .cab into the selected environment/company.\n"
            "By default first runs a local preflight (valid zip; no errors in <cab>_Build.log or the\n"
            "validation file next to it). If the install reports MetaUI layer conflicts with another\n"
            "company it stops, unless --auto-clear-layer-conflicts (which DELETES the conflicting layers\n"
            "and retries once). The --replace / --overwrite-* / --delete-previous-install /\n"
            "--override-directives flags make the install overwrite existing data."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "example:\n"
            "  python -m kinetic_devops.solutions --env MyTestEnv install exports/solutions/build/MySolution.cab"
        ),
    )
    install_parser.add_argument("cab_file", metavar="CAB_FILE", help="Path to the solution .cab package")
    install_parser.add_argument(
        "--solution-file",
        default="",
        help="Solution file name passed to GetInstallSettings (defaults to CAB filename).",
    )
    install_parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-install validation checks on package/log/validation files.",
    )
    install_parser.add_argument(
        "--replace",
        action="store_true",
        help="Enable replace profile: overwrite duplicate file/data, delete previous install, override directives.",
    )
    install_parser.add_argument(
        "--overwrite-duplicate-file",
        action="store_true",
        help="Set MainInstallSettings.AutoOverwriteDuplicateFile=true.",
    )
    install_parser.add_argument(
        "--overwrite-duplicate-data",
        action="store_true",
        help="Set MainInstallSettings.AutoOverwriteDuplicateData=true.",
    )
    install_parser.add_argument(
        "--delete-previous-install",
        action="store_true",
        help="Set MainInstallSettings.DeletePreviousInstall=true.",
    )
    install_parser.add_argument(
        "--override-directives",
        action="store_true",
        help="Set MainInstallSettings.OverrideDirectives=true.",
    )
    install_parser.add_argument(
        "--auto-clear-layer-conflicts",
        action="store_true",
        help="DESTRUCTIVE: If install reports MetaUI layer exists-in-another-company conflicts, delete "
             "tenant/cross-company layer and retry once.",
    )

    remove_parser = subparsers.add_parser(
        "remove",
        aliases=["delete"],
        help="Remove a solution definition and its membership rows",
        description=(
            "DESTRUCTIVE: deletes the Solution Workbench solution definition and its membership rows in the "
            "selected environment, then confirms it is gone. The underlying artifacts (BPMs, layers, ...) "
            "are not deleted. A missing solution is a no-op unless --require-exists."
        ),
    )
    remove_parser.add_argument("solution_id", metavar="SOLUTION_ID", help="Solution ID to remove")
    remove_parser.add_argument(
        "--require-exists",
        action="store_true",
        help="Fail if the solution does not exist instead of treating as no-op.",
    )

    KineticBaseClient.add_file_resolution_args(parser)

    args = parser.parse_args()

    service = KineticSolutionService(args.env, args.user, company_id=args.company or None)
    service.configure_file_resolution_from_args(args)

    if args.command == "list":
        result = service.list_solutions(company=args.company, page_size=args.page_size, absolute_page=args.page)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.command == "backup":
        result = service.backup_solution(
            args.solution_id,
            out_dir=args.out_dir,
            company=args.company,
            include_dynamic=not args.no_dynamic,
            include_tracked=not args.no_tracked,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.command == "recreate":
        result = service.recreate_solution_from_backup(
            args.backup_file,
            company=args.company,
            target_solution_id=args.target_solution_id,
            overwrite=args.overwrite,
            hydrate_items=args.hydrate_items,
            hydrate_tables=args.hydrate_table,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.command == "build":
        if args.out_dir:
            result = service.build_and_download(
                args.solution_id,
                out_dir=args.out_dir,
                company=args.company,
                folder=args.file_folder,
            )
        else:
            result = service.build_solution(args.solution_id, company=args.company)
        if args.out_dir:
            result["output_file"] = service.save_build_result(args.solution_id, result, args.out_dir)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    if args.command == "install":
        result = service.install_solution_cab(
            args.cab_file,
            solution_file=args.solution_file,
            company=args.company,
            skip_preflight=args.skip_preflight,
            replace=args.replace,
            overwrite_duplicate_file=args.overwrite_duplicate_file,
            overwrite_duplicate_data=args.overwrite_duplicate_data,
            delete_previous_install=args.delete_previous_install,
            override_directives=args.override_directives,
            auto_clear_layer_conflicts=args.auto_clear_layer_conflicts,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # High-visibility summary at the end for operators.
        high_vis = result.get("high_vis") or {}
        warnings = high_vis.get("warnings") or []
        failures = high_vis.get("failures") or []
        if warnings or failures:
            print("\n" + "=" * 80, file=sys.stderr)
            print("POST-INSTALL HIGHLIGHTS", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
            for item in failures:
                print(f"[FAILURE] {item.get('message', '')}", file=sys.stderr)
            for item in warnings:
                print(f"[WARNING] {item.get('message', '')}", file=sys.stderr)
            print("=" * 80, file=sys.stderr)
        return

    if args.command in {"remove", "delete"}:
        result = service.remove_solution(
            args.solution_id,
            company=args.company,
            require_exists=args.require_exists,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
