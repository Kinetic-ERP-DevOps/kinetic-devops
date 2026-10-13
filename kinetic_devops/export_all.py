"""
kinetic_devops/export_all.py

Core export orchestration for Kinetic DevOps workflows.

Supports:
- EFx library export mode (ExportAllTheThings-compatible)
- Native endpoint export mode (no ExportAllTheThings dependency)
- Auto mode (prefer EFx export library; fall back to native)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl

from kinetic_devops import _lazy_import

requests = _lazy_import("requests")

if __package__:
    from .efx import KineticEFxService
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.efx import KineticEFxService
    from kinetic_devops.base_client import KineticBaseClient

DEFAULT_EATT_LIBRARY = "ExportAllTheThings"
DEFAULT_EXPORT_FUNCTIONS = [
    "ExportAllCustomBAQs",
    "ExportAllFunctionLibraries",
    "ExportAllUDCodes",
    "ExportDataDirectivesByTable",
    "ExportDirectivesByGroups",
    "ExportMethodDirectivesByService",
    "ExportAllKineticCustomLayers",
    "ExportAllKineticSystemLayers",
]

DEFAULT_NATIVE_EXPORT_PLAN = [
    {
        "id": "baq_list",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.BO.BAQDesignerSvc/GetList",
        "body": {
            "whereClause": "SystemFlag=false",
            "pageSize": 0,
            "absolutePage": 0,
        },
        "output": "baq_list.json",
    },
    {
        "id": "efx_library_list",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.Lib.EfxLibraryDesignerSvc/GetLibraryList",
        "body": {
            "options": {
                "kind": 1,
                "startsWith": "",
                "rollOutMode": 2,
                "status": 2,
            }
        },
        "output": "efx_library_list.json",
    },
    {
        "id": "ud_codes",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.BO.UserCodesSvc/GetRows",
        "body": {
            "whereClauseUDCodeType": "",
            "whereClauseUDCodes": "",
            "pageSize": 0,
            "absolutePage": 0,
        },
        "output": "ud_codes.json",
    },
    {
        "id": "method_directives",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.BO.BpMethodSvc/GetList",
        "body": {
            "whereClause": "Source = 'BO' AND ObjectNS <> ''",
            "pageSize": 0,
            "absolutePage": 0,
        },
        "output": "method_directives.json",
    },
    {
        "id": "data_directives",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.BO.BpMethodSvc/GetList",
        "body": {
            "whereClause": "Source = 'DB'",
            "pageSize": 0,
            "absolutePage": 0,
        },
        "output": "data_directives.json",
    },
    {
        "id": "directive_groups",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.BO.BpMethodSvc/GetDirectiveGroups",
        "body": {
            "source": "BO,DB"
        },
        "output": "directive_groups.json",
    },
    {
        "id": "metafx_applications",
        "method": "POST",
        "endpoint": "/api/v2/odata/{company}/Ice.LIB.MetaFXSvc/GetApplications",
        "body": {
            "request": {
                "Type": "view",
                "SubType": "",
                "SearchText": "",
                "IncludeAllLayers": True,
                "IncludePersLayers": False,
            }
        },
        "output": "metafx_applications.json",
    },
]


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or "artifact"


def _walk_values(node: Any) -> Iterable[Any]:
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_values(item)


def _collect_function_ids_from_payload(payload: Any) -> List[str]:
    discovered: List[str] = []
    seen = set()

    for key, value in _walk_values(payload):
        if isinstance(key, str) and key.lower() == "functionid" and isinstance(value, str):
            function_id = value.strip()
            if function_id and function_id not in seen:
                seen.add(function_id)
                discovered.append(function_id)

    return discovered


def _is_probable_base64(value: str) -> bool:
    sample = value.strip()
    if not sample or len(sample) < 8:
        return False
    if len(sample) % 4 != 0:
        return False
    return re.fullmatch(r"[A-Za-z0-9+/=\\r\\n]+", sample) is not None


def _extract_file_payload(result: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[bytes], Optional[str]]:
    """
    Return (payload_key, payload_type, payload_bytes, payload_text).

    payload_type is one of: base64, text
    """
    priority_keys = ["ZipBase64", "File", "Content"]

    # First pass: direct keys with original case
    for key in priority_keys:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            if key == "ZipBase64" or _is_probable_base64(value):
                try:
                    return key, "base64", base64.b64decode(value), None
                except (binascii.Error, ValueError):
                    pass
            return key, "text", None, value

    # Second pass: recursive key search (case-insensitive)
    for key, value in _walk_values(result):
        if not isinstance(key, str):
            continue
        if key.lower() not in {"zipbase64", "file", "content"}:
            continue
        if not isinstance(value, str) or not value.strip():
            continue

        if key.lower() == "zipbase64" or _is_probable_base64(value):
            try:
                return key, "base64", base64.b64decode(value), None
            except (binascii.Error, ValueError):
                pass

        return key, "text", None, value

    return None, None, None, None


def _deep_template(value: Any, mapping: Dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key, replacement in mapping.items():
            out = out.replace("{" + key + "}", replacement)
        return out
    if isinstance(value, list):
        return [_deep_template(item, mapping) for item in value]
    if isinstance(value, dict):
        return {k: _deep_template(v, mapping) for k, v in value.items()}
    return value


def _parse_query_params(raw: str) -> Dict[str, str]:
    text = (raw or "").lstrip("?").strip()
    if not text:
        return {}
    return {k: v for k, v in parse_qsl(text, keep_blank_values=True)}


def _split_companies(raw: str) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in str(raw or "").split(","):
        company = item.strip()
        if not company:
            continue
        upper = company.upper()
        if upper in seen:
            continue
        seen.add(upper)
        out.append(company)
    return out


class KineticExportAllService(KineticEFxService):
    """Service that discovers and executes ExportAllTheThings export functions."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._last_discovery_error: Dict[str, Any] = {}

    def resolve_companies(self, explicit_companies: Optional[List[str]] = None) -> List[str]:
        if explicit_companies:
            provided = [c.strip() for c in explicit_companies if isinstance(c, str) and c.strip()]
            if provided:
                return _split_companies(",".join(provided))

        base_cfg = self.mgr.get_base_config(self.config.get("nickname", "")) or {}
        raw_companies = base_cfg.get("companies") or self.config.get("company", "")
        companies = _split_companies(str(raw_companies))
        if companies:
            return companies

        fallback = str(self.config.get("company", "")).strip()
        return [fallback] if fallback else []

    def discover_export_functions(
        self,
        library: str = DEFAULT_EATT_LIBRARY,
        company: str = "",
        fallback_to_defaults: bool = True,
    ) -> List[str]:
        target_co = company if company else self.config["company"]
        endpoint = (
            f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/"
            "Ice.Lib.EfxLibraryDesignerSvc/GetLibrary"
        )

        headers = self.mgr.get_auth_headers({**self.config, "company": target_co})
        payload = {"libraryId": library}

        self._last_discovery_error = {}

        try:
            resp = requests.post(endpoint, json=payload, headers=headers, timeout=120)
            if not resp.ok:
                self.log_wire("POST", endpoint, headers, body=payload, resp=resp)
                resp.raise_for_status()
            data = resp.json() or {}
            discovered = [fn for fn in _collect_function_ids_from_payload(data) if fn.startswith("Export")]
            if discovered:
                return sorted(set(discovered))
        except requests.HTTPError as exc:
            status_code = None
            if exc.response is not None:
                status_code = exc.response.status_code
            self._last_discovery_error = {
                "kind": "http",
                "status_code": status_code,
                "error": str(exc),
                "endpoint": endpoint,
            }
        except Exception as exc:
            self._last_discovery_error = {
                "kind": "unknown",
                "status_code": None,
                "error": str(exc),
                "endpoint": endpoint,
            }

        if self._last_discovery_error:
            # Fall back to known defaults for resilient CLI behavior.
            pass

        return list(DEFAULT_EXPORT_FUNCTIONS) if fallback_to_defaults else []

    def get_last_discovery_error(self) -> Dict[str, Any]:
        return dict(self._last_discovery_error)

    def _request_native(
        self,
        method: str,
        endpoint: str,
        params: str = "",
        body: Optional[Dict[str, Any]] = None,
        company: str = "",
    ) -> requests.Response:
        target_co = company if company else self.config["company"]
        mapping = {
            "company": target_co,
            "base_url": self.config["url"].rstrip("/"),
        }
        rendered_endpoint = _deep_template(endpoint, mapping)
        if rendered_endpoint.startswith("http://") or rendered_endpoint.startswith("https://"):
            url = rendered_endpoint
        else:
            url = f"{self.config['url'].rstrip('/')}/{rendered_endpoint.lstrip('/')}"

        headers = self.mgr.get_auth_headers({**self.config, "company": target_co})
        query = _parse_query_params(params)
        payload = _deep_template(body or {}, mapping)

        response = requests.request(
            method=method.upper(),
            url=url,
            params=query,
            json=payload if method.upper() != "GET" else None,
            headers=headers,
            timeout=180,
        )

        if not response.ok:
            self.log_wire(method.upper(), url, headers, body=payload, resp=response)
            response.raise_for_status()

        return response

    def export_native_one(
        self,
        out_dir: str,
        endpoint: str,
        method: str = "GET",
        params: str = "",
        body: Optional[Dict[str, Any]] = None,
        company: str = "",
        output_name: str = "native_export",
        dedup_registry: Optional[Dict[str, Dict[str, str]]] = None,
        dedup_namespace: str = "",
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        response = self._request_native(
            method=method,
            endpoint=endpoint,
            params=params,
            body=body,
            company=company,
        )

        content_type = (response.headers.get("content-type") or "").lower()
        base_name = _safe_name(output_name)
        output_file = ""
        content_hash = hashlib.sha256(response.content).hexdigest()
        company_name = company or self.config.get("company", "")
        namespace = dedup_namespace or output_name
        dedup_key = f"{namespace}:{content_hash}"

        if dedup_registry is not None and dedup_key in dedup_registry:
            existing = dedup_registry[dedup_key]
            return {
                "id": output_name,
                "company": company_name,
                "success": True,
                "duplicate": True,
                "duplicate_of": existing.get("output_file", ""),
                "method": method.upper(),
                "endpoint": endpoint,
                "output_file": existing.get("output_file", ""),
                "payload_type": existing.get("payload_type", ""),
                "content_hash": content_hash,
                "status_code": response.status_code,
            }

        if "application/json" in content_type:
            payload = response.json()
            output_file = os.path.join(out_dir, f"{base_name}.json")
            output_file = self.resolve_output_path(output_file, conflict_resolution="timestamp")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            payload_type = "json"
        else:
            payload_type = "binary"
            is_textual = content_type.startswith("text/") or content_type in (
                "application/xml", "application/csv", "application/xhtml+xml",
            )
            if is_textual:
                try:
                    output_file = os.path.join(out_dir, f"{base_name}.txt")
                    output_file = self.resolve_output_path(output_file, conflict_resolution="timestamp")
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(response.text)
                    payload_type = "text"
                except Exception:
                    is_textual = False

            if not is_textual:
                output_file = os.path.join(out_dir, f"{base_name}.bin")
                output_file = self.resolve_output_path(output_file, conflict_resolution="timestamp")
                with open(output_file, "wb") as f:
                    f.write(response.content)

        if dedup_registry is not None:
            dedup_registry[dedup_key] = {
                "output_file": output_file,
                "payload_type": payload_type,
            }

        return {
            "id": output_name,
            "company": company_name,
            "success": True,
            "duplicate": False,
            "duplicate_of": "",
            "method": method.upper(),
            "endpoint": endpoint,
            "output_file": output_file,
            "payload_type": payload_type,
            "content_hash": content_hash,
            "status_code": response.status_code,
        }

    def export_native_all(
        self,
        out_dir: str,
        plan: List[Dict[str, Any]],
        company: str = "",
        continue_on_error: bool = True,
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        results: List[Dict[str, Any]] = []
        failures = 0

        for item in plan:
            item_id = str(item.get("id", "native_item"))
            try:
                result = self.export_native_one(
                    out_dir=out_dir,
                    endpoint=str(item.get("endpoint", "")),
                    method=str(item.get("method", "GET")),
                    params=str(item.get("params", "")),
                    body=item.get("body") if isinstance(item.get("body"), dict) else {},
                    company=company,
                    output_name=str(item.get("output", item_id)).rsplit(".", 1)[0],
                )
                results.append(result)
            except Exception as exc:
                failures += 1
                results.append(
                    {
                        "id": item_id,
                        "success": False,
                        "method": str(item.get("method", "GET")).upper(),
                        "endpoint": str(item.get("endpoint", "")),
                        "output_file": "",
                        "error": str(exc),
                    }
                )
                if not continue_on_error:
                    break

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = os.path.join(out_dir, f"native_export_manifest_{timestamp}.json")
        manifest = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "native",
            "company": company or self.config.get("company", ""),
            "total_items": len(plan),
            "failed_items": failures,
            "successful_items": len(results) - failures,
            "results": results,
        }

        manifest_path = self.resolve_output_path(manifest_path, conflict_resolution="timestamp")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return {
            "success": failures == 0,
            "manifest": manifest_path,
            "results": results,
            "summary": manifest,
        }

    def export_native_all_companies(
        self,
        out_dir: str,
        plan: List[Dict[str, Any]],
        companies: Optional[List[str]] = None,
        continue_on_error: bool = True,
        dedup: bool = True,
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        company_list = self.resolve_companies(companies)
        dedup_registry: Optional[Dict[str, Dict[str, str]]] = {} if dedup else None

        results: List[Dict[str, Any]] = []
        company_summaries: List[Dict[str, Any]] = []
        total_failures = 0
        total_duplicates = 0

        for company in company_list:
            company_dir = os.path.join(out_dir, _safe_name(company))
            os.makedirs(company_dir, exist_ok=True)

            company_results: List[Dict[str, Any]] = []
            company_failures = 0
            company_duplicates = 0

            for item in plan:
                item_id = str(item.get("id", "native_item"))
                try:
                    result = self.export_native_one(
                        out_dir=company_dir,
                        endpoint=str(item.get("endpoint", "")),
                        method=str(item.get("method", "GET")),
                        params=str(item.get("params", "")),
                        body=item.get("body") if isinstance(item.get("body"), dict) else {},
                        company=company,
                        output_name=str(item.get("output", item_id)).rsplit(".", 1)[0],
                        dedup_registry=dedup_registry,
                        dedup_namespace=item_id,
                    )
                    if result.get("duplicate"):
                        company_duplicates += 1
                        total_duplicates += 1
                    company_results.append(result)
                except Exception as exc:
                    company_failures += 1
                    total_failures += 1
                    company_results.append(
                        {
                            "id": item_id,
                            "company": company,
                            "success": False,
                            "duplicate": False,
                            "duplicate_of": "",
                            "method": str(item.get("method", "GET")).upper(),
                            "endpoint": str(item.get("endpoint", "")),
                            "output_file": "",
                            "error": str(exc),
                        }
                    )
                    if not continue_on_error:
                        break

            results.extend(company_results)
            company_summaries.append(
                {
                    "company": company,
                    "total_items": len(company_results),
                    "failed_items": company_failures,
                    "duplicate_items": company_duplicates,
                    "successful_items": len([r for r in company_results if r.get("success")]),
                }
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = os.path.join(out_dir, f"native_export_manifest_all_companies_{timestamp}.json")
        manifest = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "native-all-companies",
            "companies": company_list,
            "dedup": dedup,
            "total_companies": len(company_list),
            "total_items": len(results),
            "failed_items": total_failures,
            "duplicate_items": total_duplicates,
            "successful_items": len(results) - total_failures,
            "company_summaries": company_summaries,
            "results": results,
        }

        manifest_path = self.resolve_output_path(manifest_path, conflict_resolution="timestamp")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return {
            "success": total_failures == 0,
            "manifest": manifest_path,
            "results": results,
            "summary": manifest,
        }

    def export_one(
        self,
        function_id: str,
        out_dir: str,
        library: str = DEFAULT_EATT_LIBRARY,
        company: str = "",
        input_data: Optional[Dict[str, Any]] = None,
        extension: str = "zip",
        dedup_registry: Optional[Dict[str, Dict[str, str]]] = None,
        dedup_namespace: str = "",
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)

        result = self.run_function(
            library=library,
            function=function_id,
            input_data=input_data or {},
            company=company,
        )

        payload_key, payload_type, payload_bytes, payload_text = _extract_file_payload(result)
        if not payload_key:
            return {
                "function_id": function_id,
                "company": company or self.config.get("company", ""),
                "success": False,
                "duplicate": False,
                "duplicate_of": "",
                "output_file": "",
                "payload_key": "",
                "error": "No file-like payload found in EFx response.",
                "response": result,
            }

        if payload_type == "base64" and payload_bytes is not None:
            content_bytes = payload_bytes
        else:
            content_bytes = (payload_text or "").encode("utf-8")

        content_hash = hashlib.sha256(content_bytes).hexdigest()
        namespace = dedup_namespace or function_id
        dedup_key = f"{namespace}:{content_hash}"

        if dedup_registry is not None and dedup_key in dedup_registry:
            existing = dedup_registry[dedup_key]
            return {
                "function_id": function_id,
                "company": company or self.config.get("company", ""),
                "success": True,
                "duplicate": True,
                "duplicate_of": existing.get("output_file", ""),
                "output_file": existing.get("output_file", ""),
                "payload_key": payload_key,
                "payload_type": payload_type,
                "content_hash": content_hash,
                "response": result,
            }

        safe_extension = _safe_name(extension).lstrip("._") or "bin"
        out_name = f"{_safe_name(function_id)}.{safe_extension if payload_type == 'base64' else 'txt'}"
        output_path = os.path.join(out_dir, out_name)
        output_path = self.resolve_output_path(output_path, conflict_resolution="timestamp")

        if payload_type == "base64" and payload_bytes is not None:
            with open(output_path, "wb") as f:
                f.write(payload_bytes)
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(payload_text or "")

        if dedup_registry is not None:
            dedup_registry[dedup_key] = {
                "output_file": output_path,
                "payload_type": payload_type,
            }

        return {
            "function_id": function_id,
            "company": company or self.config.get("company", ""),
            "success": True,
            "duplicate": False,
            "duplicate_of": "",
            "output_file": output_path,
            "payload_key": payload_key,
            "payload_type": payload_type,
            "content_hash": content_hash,
            "response": result,
        }

    def export_all(
        self,
        out_dir: str,
        library: str = DEFAULT_EATT_LIBRARY,
        company: str = "",
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        input_data: Optional[Dict[str, Any]] = None,
        continue_on_error: bool = True,
        extension: str = "zip",
    ) -> Dict[str, Any]:
        functions = self.discover_export_functions(library=library, company=company)

        if include:
            allowed = {name.strip() for name in include if name.strip()}
            functions = [fn for fn in functions if fn in allowed]

        if exclude:
            denied = {name.strip() for name in exclude if name.strip()}
            functions = [fn for fn in functions if fn not in denied]

        results: List[Dict[str, Any]] = []
        failures = 0

        for function_id in functions:
            try:
                item = self.export_one(
                    function_id=function_id,
                    out_dir=out_dir,
                    library=library,
                    company=company,
                    input_data=input_data,
                    extension=extension,
                )
                if not item.get("success", False):
                    failures += 1
                    if not continue_on_error:
                        results.append(item)
                        break
                results.append(item)
            except Exception as exc:
                failures += 1
                error_item = {
                    "function_id": function_id,
                    "success": False,
                    "output_file": "",
                    "error": str(exc),
                }
                results.append(error_item)
                if not continue_on_error:
                    break

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = os.path.join(out_dir, f"export_manifest_{timestamp}.json")
        manifest = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "library": library,
            "company": company or self.config.get("company", ""),
            "total_functions": len(functions),
            "failed_functions": failures,
            "successful_functions": len(results) - failures,
            "results": [
                {
                    "function_id": item.get("function_id", ""),
                    "success": item.get("success", False),
                    "output_file": item.get("output_file", ""),
                    "payload_key": item.get("payload_key", ""),
                    "payload_type": item.get("payload_type", ""),
                    "error": item.get("error", ""),
                }
                for item in results
            ],
        }

        os.makedirs(out_dir, exist_ok=True)
        manifest_path = self.resolve_output_path(manifest_path, conflict_resolution="timestamp")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return {
            "success": failures == 0,
            "manifest": manifest_path,
            "results": results,
            "summary": manifest,
        }

    def export_all_companies(
        self,
        out_dir: str,
        library: str = DEFAULT_EATT_LIBRARY,
        companies: Optional[List[str]] = None,
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        input_data: Optional[Dict[str, Any]] = None,
        continue_on_error: bool = True,
        extension: str = "zip",
        dedup: bool = True,
    ) -> Dict[str, Any]:
        os.makedirs(out_dir, exist_ok=True)
        company_list = self.resolve_companies(companies)
        dedup_registry: Optional[Dict[str, Dict[str, str]]] = {} if dedup else None

        results: List[Dict[str, Any]] = []
        company_summaries: List[Dict[str, Any]] = []
        total_failures = 0
        total_duplicates = 0

        for company in company_list:
            company_dir = os.path.join(out_dir, _safe_name(company))
            os.makedirs(company_dir, exist_ok=True)

            functions = self.discover_export_functions(library=library, company=company)

            if include:
                allowed = {name.strip() for name in include if name.strip()}
                functions = [fn for fn in functions if fn in allowed]

            if exclude:
                denied = {name.strip() for name in exclude if name.strip()}
                functions = [fn for fn in functions if fn not in denied]

            company_results: List[Dict[str, Any]] = []
            company_failures = 0
            company_duplicates = 0

            for function_id in functions:
                try:
                    item = self.export_one(
                        function_id=function_id,
                        out_dir=company_dir,
                        library=library,
                        company=company,
                        input_data=input_data,
                        extension=extension,
                        dedup_registry=dedup_registry,
                        dedup_namespace=function_id,
                    )
                    if item.get("duplicate"):
                        company_duplicates += 1
                        total_duplicates += 1
                    company_results.append(item)
                    if not item.get("success", False):
                        company_failures += 1
                        total_failures += 1
                        if not continue_on_error:
                            break
                except Exception as exc:
                    company_failures += 1
                    total_failures += 1
                    company_results.append(
                        {
                            "function_id": function_id,
                            "company": company,
                            "success": False,
                            "duplicate": False,
                            "duplicate_of": "",
                            "output_file": "",
                            "error": str(exc),
                        }
                    )
                    if not continue_on_error:
                        break

            results.extend(company_results)
            company_summaries.append(
                {
                    "company": company,
                    "total_functions": len(functions),
                    "failed_functions": company_failures,
                    "duplicate_functions": company_duplicates,
                    "successful_functions": len([r for r in company_results if r.get("success")]),
                }
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifest_path = os.path.join(out_dir, f"export_manifest_all_companies_{timestamp}.json")
        manifest = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": "eatt-all-companies",
            "library": library,
            "companies": company_list,
            "dedup": dedup,
            "total_companies": len(company_list),
            "total_functions": len(results),
            "failed_functions": total_failures,
            "duplicate_functions": total_duplicates,
            "successful_functions": len(results) - total_failures,
            "company_summaries": company_summaries,
            "results": [
                {
                    "function_id": item.get("function_id", ""),
                    "company": item.get("company", ""),
                    "success": item.get("success", False),
                    "duplicate": item.get("duplicate", False),
                    "duplicate_of": item.get("duplicate_of", ""),
                    "output_file": item.get("output_file", ""),
                    "payload_key": item.get("payload_key", ""),
                    "payload_type": item.get("payload_type", ""),
                    "content_hash": item.get("content_hash", ""),
                    "error": item.get("error", ""),
                }
                for item in results
            ],
        }

        manifest_path = self.resolve_output_path(manifest_path, conflict_resolution="timestamp")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return {
            "success": total_failures == 0,
            "manifest": manifest_path,
            "results": results,
            "summary": manifest,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export everything from Kinetic (with or without ExportAllTheThings).\n"
            "Downloads customization artifacts (BAQs, EFx libraries, UD codes, BPM directives, layers, ...)\n"
            "into a local directory and writes a JSON manifest. Modes:\n"
            "  eatt    call every Export* function of the ExportAllTheThings EFx library and save its output\n"
            "  native  call standard Kinetic REST endpoints from a built-in (or --native-plan) request plan\n"
            "  auto    use eatt if the library's Export* functions are discoverable, else native (default;\n"
            "          stops instead of falling back if discovery is rejected with 401/403)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.export_all --env MyTestEnv --list\n"
            "  python -m kinetic_devops.export_all --env MyTestEnv --function ExportAllCustomBAQs\n"
            "  python -m kinetic_devops.export_all --env MyTestEnv --all-companies --out-dir exports/all"
        ),
    )
    parser.add_argument(
        "--mode", choices=["auto", "eatt", "native"], default="auto",
        help="Export strategy: auto, eatt or native (see above; default: auto).",
    )
    parser.add_argument(
        "--env", metavar="ENV",
        help="Environment nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument("--user", metavar="USER_ID", help="User ID for session")
    parser.add_argument("--co", metavar="COMPANY", help="Company override (default: the session's company)", default="")
    parser.add_argument(
        "--all-companies", action="store_true",
        help="Run exports for each configured company, into one <out-dir>/<company> folder per company",
    )
    parser.add_argument(
        "--companies", nargs="*", metavar="COMPANY", default=None,
        help="Optional explicit company list for --all-companies (default: the environment's configured companies)",
    )
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Disable content-hash de-duplication in --all-companies mode (by default an artifact identical "
             "to one already exported for another company is not written again)",
    )
    parser.add_argument("--library", metavar="LIBRARY", default=DEFAULT_EATT_LIBRARY, help="EFx library name (default: %(default)s)")
    parser.add_argument(
        "--out-dir", metavar="DIR", default=os.path.join("exports", "ExportAllTheThings"),
        help="Output directory, created if missing (default: %(default)s)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List discovered export functions only (native mode: the built-in plan IDs); exports nothing",
    )
    parser.add_argument("--function", metavar="FUNCTION", default="", help="Run only one export function (eatt mode)")
    parser.add_argument("--include", nargs="*", metavar="FUNCTION", help="Optional include list for bulk export (eatt mode)")
    parser.add_argument("--exclude", nargs="*", metavar="FUNCTION", help="Optional exclude list for bulk export (eatt mode)")
    parser.add_argument(
        "--input", metavar="JSON", default="{}",
        help="JSON object payload passed to export functions (default: {})",
    )
    parser.add_argument(
        "--extension", metavar="EXT", default="zip",
        help="Artifact extension for base64 payloads returned by export functions (default: zip)",
    )
    parser.add_argument("--stop-on-error", action="store_true", help="Stop bulk export on first error (default: continue)")
    parser.add_argument(
        "--native-plan", metavar="PLAN_JSON", default="",
        help="Path to JSON file containing native endpoint plan list (objects with id/method/endpoint/params/"
             "body/output); default: the built-in plan",
    )
    parser.add_argument(
        "--native-endpoint", metavar="URL", default="",
        help="Single native endpoint to export (relative or absolute URL); {company} is substituted. The "
             "request is sent as given, so a non-read method can change data.",
    )
    parser.add_argument("--native-method", metavar="METHOD", default="GET", help="HTTP method for --native-endpoint (default: GET)")
    parser.add_argument("--native-params", metavar="QUERY", default="", help="Query string for --native-endpoint")
    parser.add_argument("--native-body", metavar="JSON", default="{}", help="JSON object body for --native-endpoint (default: {})")
    parser.add_argument(
        "--native-name", metavar="NAME", default="native_export",
        help="Output base name for --native-endpoint (default: native_export)",
    )
    KineticBaseClient.add_file_resolution_args(parser)

    args = parser.parse_args()

    try:
        input_data = json.loads(args.input)
        if not isinstance(input_data, dict):
            raise ValueError("--input must parse to a JSON object")
    except Exception as exc:
        print(f"Error: invalid --input JSON: {exc}")
        sys.exit(2)

    try:
        native_body = json.loads(args.native_body)
        if not isinstance(native_body, dict):
            raise ValueError("--native-body must parse to a JSON object")
    except Exception as exc:
        print(f"Error: invalid --native-body JSON: {exc}")
        sys.exit(2)

    service = KineticExportAllService(args.env, args.user, debug=False)
    service.configure_file_resolution_from_args(args)

    effective_mode = args.mode
    discovered_functions: List[str] = []
    if args.mode in {"auto", "eatt"}:
        discovered_functions = service.discover_export_functions(
            library=args.library,
            company=args.co,
            fallback_to_defaults=(args.mode == "eatt"),
        )
        if args.mode == "auto":
            discovery_error = service.get_last_discovery_error()
            if discovery_error.get("kind") == "http" and discovery_error.get("status_code") in {401, 403}:
                print(json.dumps({
                    "mode": "auto",
                    "success": False,
                    "error": "ExportAllTheThings discovery failed with authentication/authorization error; refusing native fallback.",
                    "discovery_error": discovery_error,
                }, indent=2, ensure_ascii=False))
                sys.exit(1)
            effective_mode = "eatt" if discovered_functions else "native"

    if effective_mode == "eatt":
        if args.list:
            if args.all_companies:
                companies = service.resolve_companies(args.companies)
                functions_by_company = {
                    co: service.discover_export_functions(library=args.library, company=co)
                    for co in companies
                }
                print(json.dumps({
                    "mode": "eatt",
                    "library": args.library,
                    "all_companies": True,
                    "companies": companies,
                    "functions_by_company": functions_by_company,
                }, indent=2))
            else:
                print(json.dumps({"mode": "eatt", "library": args.library, "functions": discovered_functions}, indent=2))
            return

        if args.function and not args.all_companies:
            result = service.export_one(
                function_id=args.function,
                out_dir=args.out_dir,
                library=args.library,
                company=args.co,
                input_data=input_data,
                extension=args.extension,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            sys.exit(0 if result.get("success") else 1)

        if args.all_companies:
            include = list(args.include or [])
            if args.function:
                include = [args.function]

            outcome = service.export_all_companies(
                out_dir=args.out_dir,
                library=args.library,
                companies=args.companies,
                include=include,
                exclude=args.exclude,
                input_data=input_data,
                continue_on_error=not args.stop_on_error,
                extension=args.extension,
                dedup=not args.no_dedup,
            )
        else:
            outcome = service.export_all(
                out_dir=args.out_dir,
                library=args.library,
                company=args.co,
                include=args.include,
                exclude=args.exclude,
                input_data=input_data,
                continue_on_error=not args.stop_on_error,
                extension=args.extension,
            )
    else:
        if args.list:
            payload = {
                "mode": "native",
                "default_plan_ids": [item.get("id", "") for item in DEFAULT_NATIVE_EXPORT_PLAN],
            }
            if args.all_companies:
                payload["all_companies"] = True
                payload["companies"] = service.resolve_companies(args.companies)
            print(json.dumps(payload, indent=2))
            return

        if args.native_endpoint:
            if args.all_companies:
                outcome = service.export_native_all_companies(
                    out_dir=args.out_dir,
                    companies=args.companies,
                    continue_on_error=not args.stop_on_error,
                    dedup=not args.no_dedup,
                    plan=[
                        {
                            "id": args.native_name,
                            "method": args.native_method,
                            "endpoint": args.native_endpoint,
                            "params": args.native_params,
                            "body": native_body,
                            "output": args.native_name,
                        }
                    ],
                )
            else:
                single = service.export_native_one(
                    out_dir=args.out_dir,
                    endpoint=args.native_endpoint,
                    method=args.native_method,
                    params=args.native_params,
                    body=native_body,
                    company=args.co,
                    output_name=args.native_name,
                )
                print(json.dumps(single, indent=2, ensure_ascii=False))
                sys.exit(0 if single.get("success") else 1)
        else:
            if args.native_plan:
                with open(args.native_plan, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if not isinstance(loaded, list):
                    print("Error: --native-plan JSON must be a list of request objects")
                    sys.exit(2)
                plan = loaded
            else:
                plan = DEFAULT_NATIVE_EXPORT_PLAN

            if args.all_companies:
                outcome = service.export_native_all_companies(
                    out_dir=args.out_dir,
                    plan=plan,
                    companies=args.companies,
                    continue_on_error=not args.stop_on_error,
                    dedup=not args.no_dedup,
                )
            else:
                outcome = service.export_native_all(
                    out_dir=args.out_dir,
                    plan=plan,
                    company=args.co,
                    continue_on_error=not args.stop_on_error,
                )

    print(json.dumps({
        "mode": effective_mode,
        "success": outcome.get("success", False),
        "manifest": outcome.get("manifest", ""),
        "summary": outcome.get("summary", {}),
    }, indent=2, ensure_ascii=False))
    sys.exit(0 if outcome.get("success", False) else 1)


if __name__ == "__main__":
    main()
