"""EFx Function Library management via Ice.Lib.EfxLibraryDesignerSvc.

Method names and payload shapes below are verified against captured browser
traces of the EFx Studio UI (Export Library / Import Library actions), not
guessed from documentation -- Ice.Lib.EfxLibraryDesignerSvc isn't documented
consistently across Epicor instances. Only the methods actually exercised in
a trace are implemented; Promote/Demote/Assign Companies/Delete/References
are intentionally not included yet -- they need their own captured trace
before being wrapped, to avoid shipping invented payload shapes.
"""

import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

if __package__:
    from .base_client import KineticBaseClient
    from .artifact_validation import normalize_artifact_rows, DEFAULT_IGNORE_FIELDS
else:
    from kinetic_devops.base_client import KineticBaseClient
    from kinetic_devops.artifact_validation import normalize_artifact_rows, DEFAULT_IGNORE_FIELDS


SERVICE_NAME = "Ice.Lib.EfxLibraryDesignerSvc"


def _safe_path_component(value: str) -> str:
    """Strip path separators and traversal sequences from a server-supplied
    ID before using it as a directory/filename component. The original,
    unmodified value should still be used for API calls -- this is only
    for constructing local output paths safely."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip(".") or "library"
    return cleaned


def _node_file_name(display_name: str, index: int, used_names: Dict[str, int]) -> str:
    """Build a stable .cs filename for a code node from its DisplayName --
    deliberately NOT from its NodeId/GUID. A DirectiveStep's Id is assigned
    per-instance; the 'same' logical function created independently in two
    environments (or recreated via import) can end up with different
    NodeIds for what's otherwise identical code. Embedding the GUID in the
    filename would silently break cross-environment push-back: push would
    rebuild the expected filename from the freshly-fetched (target
    environment's) live GUID, find no local file with that exact name, and
    skip the node without even raising -- the edit would just silently not
    apply. DisplayName is stable across environments for the same logical
    node (it's user-authored content, not a generated identity), so it's
    used as the correlation key on both extract and push; the actual live
    NodeId is always re-read fresh from the current fetch for the API call
    itself, never trusted from a stored/stale value.

    used_names tracks how many times each base name has been seen so far
    in this call, appending a numeric suffix for duplicates (e.g. two
    nodes both named "Execute Custom Code 0" -- rare but not impossible)."""
    base = _safe_path_component(display_name) if display_name else f"CustomCode{index}"
    count = used_names.get(base, 0)
    used_names[base] = count + 1
    return f"{base}.cs" if count == 0 else f"{base}_{count}.cs"


def _resolve_contained_path(out_dir: str, *parts: str) -> str:
    """Join parts under out_dir and verify the result doesn't escape it."""
    out_dir_abs = os.path.abspath(out_dir)
    candidate = os.path.abspath(os.path.join(out_dir_abs, *parts))
    if candidate != out_dir_abs and not candidate.startswith(out_dir_abs + os.sep):
        raise ValueError(f"Resolved path '{candidate}' escapes output directory '{out_dir_abs}'.")
    return candidate


def _local_tag(tag: str) -> str:
    """Strip the XML namespace off an ElementTree tag ('{ns}Name' -> 'Name')."""
    return tag.rsplit("}", 1)[-1]


def _xml_parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _iter_custom_code_actions(root: ET.Element):
    """Yield (directive_step_or_None, action_element) for every
    CustomCodeAction node in a function Body XML tree, regardless of
    namespace prefix or nesting depth (conditions/branches can nest
    DirectiveSteps inside each other)."""
    parent_map = _xml_parent_map(root)
    for action in root.iter():
        if _local_tag(action.tag) != "CustomCodeAction":
            continue
        step = None
        node = action
        while node in parent_map:
            node = parent_map[node]
            if _local_tag(node.tag) == "DirectiveStep":
                step = node
                break
        yield step, action


def _dotnet_xml_attribute_escape(text: str) -> str:
    """Match .NET's XmlWriter attribute-value escaping exactly (observed in
    live Body XML): <, >, &, " are entity-escaped, and \\n/\\r/\\t become
    numeric character references so whitespace survives attribute-value
    normalization. Used instead of a generic XML library so a push-back
    edit only ever touches the one attribute value being changed."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    text = text.replace("\r\n", "\n")
    text = text.replace("\n", "&#xA;").replace("\r", "&#xD;").replace("\t", "&#x9;")
    return text


def _diagnostic_is_blocking(diagnostic: str) -> bool:
    """Classify one ApplyChangesWithDiagnostics diagnostic string. These
    come back as plain compiler-output text (e.g. "file.cs(12,3): warning
    ECF1002: ..."), not structured objects with a severity field, so
    classification is substring-based on the standard C# compiler format.
    Verified live: a warning-level diagnostic did not block the save (the
    server applied it anyway); unrecognized formats are treated as
    blocking (fail closed) since that hasn't been observed/verified."""
    lowered = str(diagnostic).lower()
    if ": warning " in lowered:
        return False
    return True


def _build_new_signature_rows(
    library_id: str,
    function_id: str,
    existing_rows: List[Dict[str, Any]],
    parameters: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Build new EfxFunctionSignature rows for push_function_source,
    auto-numbering ParameterID/Order per (FunctionID, Response) group,
    continuing from the highest existing value. Pure/no I/O so it's unit
    testable without live credentials."""
    new_rows: List[Dict[str, Any]] = []
    for param in parameters:
        response = bool(param.get("response", False))
        same_group = [r for r in existing_rows if bool(r.get("Response")) == response]
        same_group += [r for r in new_rows if bool(r.get("Response")) == response]
        next_id = max([int(r.get("ParameterID") or 0) for r in same_group], default=0) + 1
        next_order = max([int(r.get("Order") or 0) for r in same_group], default=0) + 1
        new_rows.append(
            {
                "LibraryID": library_id,
                "FunctionID": function_id,
                "Response": response,
                "ParameterID": next_id,
                "ArgumentName": param["argument_name"],
                "Order": next_order,
                "DataType": param["data_type"],
                "DataTypeInfo": param.get("data_type_info"),
                "Optional": bool(param.get("optional", False)),
                "DefaultValue": param.get("default_value"),
                "Description": param.get("description", ""),
                "RowMod": "A",
            }
        )
    return new_rows


def _has_metadata_changes(row: Dict[str, Any], metadata: Dict[str, Any]) -> bool:
    """True if any key in metadata would actually change row's current
    value -- an empty metadata dict, or one that only repeats existing
    values, is not a change. Pure/no I/O so it's unit testable."""
    return bool(metadata) and any(row.get(k) != v for k, v in metadata.items())


def _build_new_ref_table_rows(
    library_id: str,
    existing_rows: List[Dict[str, Any]],
    table_ids: List[str],
) -> List[Dict[str, Any]]:
    """Build new EfxRefTable rows for push_function_source, skipping any
    TableID already referenced. Pure/no I/O so it's unit testable without
    live credentials."""
    existing_ids = {r.get("TableID") for r in existing_rows}
    return [
        {"LibraryID": library_id, "TableID": table_id, "Updatable": False, "RowMod": "A"}
        for table_id in table_ids
        if table_id not in existing_ids
    ]


def _replace_code_attribute_for_step(body: str, step_id: str, new_code: str) -> str:
    """Replace the Code="..." attribute inside the DirectiveStep whose Id
    matches step_id, via targeted string surgery on the raw Body text --
    NOT a parse-then-reserialize round-trip. Body is .NET XAML
    serialization (DirectiveDefinition2); verified live that reserializing
    the whole tree via a generic XML library (even just parsing and
    writing it back unchanged) breaks the server's XAML deserializer
    ('Unable to read function definition' / DeserializeBody) because it
    rewrites namespace prefixes and formatting the deserializer is
    sensitive to. Editing only the one attribute's text in place keeps
    everything else byte-identical to what the server produced."""
    step_pattern = re.compile(r'<DirectiveStep\b[^>]*\bId="' + re.escape(step_id) + r'"[^>]*>')
    match = step_pattern.search(body)
    if not match:
        raise ValueError(f"DirectiveStep with Id '{step_id}' not found in Body.")
    start = match.start()
    # (?=[\s>/]) excludes XAML property-element tags like <DirectiveStep.Action>
    # and <DirectiveStep.VisualProperties>, which aren't sibling step boundaries.
    next_step = re.compile(r"<DirectiveStep(?=[\s>/])").search(body, match.end())
    end = next_step.start() if next_step else len(body)
    segment = body[start:end]

    code_pattern = re.compile(r'(Code=")([^"]*)(")')
    if not code_pattern.search(segment):
        raise ValueError(f"No Code attribute found for DirectiveStep '{step_id}'.")
    new_segment = code_pattern.sub(
        lambda m: m.group(1) + _dotnet_xml_attribute_escape(new_code) + m.group(3),
        segment,
        count=1,
    )
    return body[:start] + new_segment + body[end:]


# ---------------------------------------------------------------------------
# Package-based deployment (plan/deploy).
#
# ImportLibrary behavior below was verified live on a Kinetic Cloud test
# environment (2026-10-12, package format Version 5.2.100) with a throwaway
# library: a package must start with a UTF-8 BOM ("Invalid library
# export document" otherwise); OverwriteMode 0 refuses an existing library,
# 1 replaces it in full (parameters/references/SharedWithGroup absent from
# the package are removed, so re-importing an older package is an exact
# rollback); import over a published library works without a demote; and the
# package's Published flag is applied (true = promoted in the same call).
# Omitted GlobalID/OriginalID are NOT assigned by import, so a replace always
# carries the deployed library's own IDs.
# ---------------------------------------------------------------------------

OVERWRITE_NONE = 0
OVERWRITE_REPLACE = 1

# Per-environment library fields, owned by the environment repo's
# deployment.json. GlobalID/OriginalID are deliberately absent: they are
# always taken from the live target, never from a record.
ENV_LIBRARY_FIELDS = ("Published", "Disabled", "Owner", "OwnedByCompany", "SharedWithGroup", "LibraryMappings")
# Environment-neutral fields owned by the source repo.
SOURCE_LIBRARY_FIELDS = ("Description", "Notes")
SOURCE_FUNCTION_FIELDS = ("Description", "Kind", "RequireTransaction", "SingleRowMode", "Private")
# Not compared by plan: system-maintained, or derived from other fields.
NOISE_LIBRARY_FIELDS = {"Revision"}
NOISE_FUNCTION_FIELDS = {"Thumbnail", "Invalid"}


def encode_package(package: Dict[str, Any]) -> bytes:
    """Serialize a library package the way ImportLibrary accepts it: UTF-8
    JSON with a leading BOM (required -- verified live)."""
    return b"\xef\xbb\xbf" + json.dumps(package, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_package(data: Any) -> Dict[str, Any]:
    """Parse ExportLibrary output (base64 string) or raw package bytes/text."""
    if isinstance(data, str) and not data.lstrip("﻿").lstrip().startswith("{"):
        data = base64.b64decode(data)
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig")
    return json.loads(data.lstrip("﻿"))


def _git(repo: str, *args: str, input_bytes: Optional[bytes] = None, env: Optional[Dict[str, str]] = None) -> bytes:
    import subprocess

    result = subprocess.run(
        ["git", "-C", repo, *args],
        input=input_bytes,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _git_show(repo: str, ref: str, path: str) -> Optional[bytes]:
    """Return a file's bytes at ref, or None if it doesn't exist there."""
    try:
        return _git(repo, "show", f"{ref}:{path}")
    except RuntimeError:
        return None


def _git_ref_exists(repo: str, ref: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return True
    except RuntimeError:
        return False


def _git_commit_files(repo: str, branch: str, files: Dict[str, bytes], message: str, start_point: str) -> str:
    """Commit files onto branch without touching the repo's working tree or
    index (temporary index + commit-tree + update-ref). The branch is
    created from start_point if it doesn't exist yet."""
    import tempfile

    parent = branch if _git_ref_exists(repo, branch) else start_point
    parent_sha = _git(repo, "rev-parse", f"{parent}^{{commit}}").decode().strip()
    fd, index_path = tempfile.mkstemp(prefix="kinetic-env-index-")
    os.close(fd)
    os.remove(index_path)  # git creates it; an empty pre-existing file is rejected as corrupt
    env = {"GIT_INDEX_FILE": index_path}
    try:
        _git(repo, "read-tree", parent_sha, env=env)
        for path, content in files.items():
            blob = _git(repo, "hash-object", "-w", "--stdin", input_bytes=content).decode().strip()
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = _git(repo, "write-tree", env=env).decode().strip()
        commit = _git(repo, "commit-tree", tree, "-p", parent_sha, "-F", "-", input_bytes=message.encode()).decode().strip()
        _git(repo, "update-ref", f"refs/heads/{branch}", commit, parent_sha if parent == branch else "")
        return commit
    finally:
        if os.path.exists(index_path):
            os.remove(index_path)


def read_library_source(source_repo: str, ref: str, path: str) -> Dict[str, Any]:
    """Read a library's environment-neutral source at a git ref:
    <path>/_library.json (optional) and one <path>/<FunctionID>/ directory
    per function holding _meta.json plus one .cs file per code node."""
    if not _git_ref_exists(source_repo, ref):
        raise ValueError(f"Source ref '{ref}' not found in {source_repo} -- fetch it first.")
    library_json = _git_show(source_repo, ref, f"{path}/_library.json")
    library = json.loads(library_json.decode("utf-8-sig")) if library_json else {}

    functions: Dict[str, Dict[str, Any]] = {}
    listing = _git(source_repo, "ls-tree", "--name-only", f"{ref}:{path}").decode().splitlines()
    for name in listing:
        meta_raw = _git_show(source_repo, ref, f"{path}/{name}/_meta.json")
        if meta_raw is None:
            continue
        meta = json.loads(meta_raw.decode("utf-8-sig"))
        files = {}
        for file_name in _git(source_repo, "ls-tree", "--name-only", f"{ref}:{path}/{name}").decode().splitlines():
            if file_name.endswith(".cs"):
                files[file_name] = _git(source_repo, "show", f"{ref}:{path}/{name}/{file_name}").decode("utf-8-sig")
        functions[meta.get("FunctionID") or name] = {"meta": meta, "files": files}
    return {"library": library, "functions": functions}


def _source_code_for_node(source_fn: Dict[str, Any], display_name: str, node_id: str, file_name: str) -> Optional[str]:
    """Find a node's code in a function's source files. Matched by
    DisplayName via _meta.json's Nodes list, then by the DisplayName-derived
    file name, then by the older '<NodeId>__<file name>' layout."""
    files = source_fn["files"]
    for node in source_fn["meta"].get("Nodes") or []:
        if node.get("DisplayName") == display_name and node.get("File") in files:
            return files[node["File"]]
    for candidate in (file_name, f"{node_id}__{file_name}"):
        if candidate in files:
            return files[candidate]
    return None


def build_package_from_target(
    target_package: Dict[str, Any], source: Dict[str, Any], deployment: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Build a deployable package on top of the target's own export
    ("--base target"): the target's IDs and anything the source doesn't
    describe are kept as deployed; code, function definitions and library
    Description/Notes come from source; per-environment settings come from
    the deployment record. Returns (package, notes)."""
    import copy

    package = copy.deepcopy(target_package)
    library = package["Library"]
    notes: List[str] = []

    for field in SOURCE_LIBRARY_FIELDS:
        if source["library"].get(field) is not None:
            library[field] = source["library"][field]
    if "References" in source["library"]:
        library["LibraryReferences"] = source["library"]["References"]

    for field in ENV_LIBRARY_FIELDS:
        if field in (deployment.get("target") or {}):
            value = deployment["target"][field]
            if value is None:
                library.pop(field, None)  # exports omit null fields
            else:
                library[field] = value

    override = (deployment.get("compatibility") or {}).get("override")
    if override:
        if override.get("Version"):
            package["Version"] = override["Version"]
        if override.get("EpicorVersion"):
            library["EpicorVersion"] = override["EpicorVersion"]
        notes.append(f"compatibility override applied: {override}")

    deployed_ids = {f.get("FunctionID") for f in library.get("Functions") or []}
    missing = sorted(set(source["functions"]) - deployed_ids)
    if missing:
        raise ValueError(
            f"Function(s) {missing} exist in source but not in the target; adding functions needs a "
            "full-fidelity source build (--base source), which isn't implemented yet."
        )

    for function in library.get("Functions") or []:
        function_id = function.get("FunctionID")
        source_fn = source["functions"].get(function_id)
        if source_fn is None:
            notes.append(f"{function_id}: not in source, kept as deployed")
            continue
        for field in SOURCE_FUNCTION_FIELDS:
            if field in source_fn["meta"]:
                function[field] = source_fn["meta"][field]
        body = function.get("Body") or ""
        if not body.lstrip().startswith("<"):
            raise ValueError(f"{function_id}: Body is not in the XML wire format; not supported.")
        used_names: Dict[str, int] = {}
        for index, (step, action) in enumerate(_iter_custom_code_actions(ET.fromstring(body))):
            if step is None:
                continue
            display_name = step.get("DisplayName") or ""
            node_id = step.get("Id") or ""
            file_name = _node_file_name(display_name, index, used_names)
            code = _source_code_for_node(source_fn, display_name, node_id, file_name)
            if code is None:
                notes.append(f"{function_id}/{display_name}: no source file, code kept as deployed")
            elif code != (action.get("Code") or ""):
                body = _replace_code_attribute_for_step(body, node_id, code)
        function["Body"] = body
    return package, notes


def _function_code(function: Dict[str, Any]) -> Dict[str, str]:
    body = function.get("Body") or ""
    if not body.lstrip().startswith("<"):
        return {"<body>": body}
    return {
        (step.get("DisplayName") if step is not None else "") or f"node{index}": action.get("Code") or ""
        for index, (step, action) in enumerate(_iter_custom_code_actions(ET.fromstring(body)))
    }


def diff_packages(before: Dict[str, Any], after: Dict[str, Any], show_code: bool = False) -> List[str]:
    """Human-readable differences between two library packages, ignoring
    noise fields. Empty list = nothing to deploy."""
    import difflib

    changes: List[str] = []
    if before.get("Version") != after.get("Version"):
        changes.append(f"package Version: {before.get('Version')!r} -> {after.get('Version')!r}")
    lib_a, lib_b = before.get("Library") or {}, after.get("Library") or {}
    structured = {"Functions", "LibraryReferences", "LibraryMappings"}
    for key in sorted((set(lib_a) | set(lib_b)) - structured - NOISE_LIBRARY_FIELDS):
        if lib_a.get(key) != lib_b.get(key):
            changes.append(f"library {key}: {lib_a.get(key)!r} -> {lib_b.get(key)!r}")

    def ref_key(r):
        return (r.get("ReferenceType"), r.get("ReferenceID"))

    refs_a = {ref_key(r): r for r in lib_a.get("LibraryReferences") or []}
    refs_b = {ref_key(r): r for r in lib_b.get("LibraryReferences") or []}
    for key in sorted(set(refs_a) | set(refs_b), key=str):
        if key not in refs_b:
            changes.append(f"reference removed: {key[1]}")
        elif key not in refs_a:
            changes.append(f"reference added: {key[1]}")
        elif refs_a[key] != refs_b[key]:
            changes.append(f"reference changed: {key[1]} {refs_a[key]} -> {refs_b[key]}")

    maps_a = {m.get("Company"): m.get("Allowed") for m in lib_a.get("LibraryMappings") or []}
    maps_b = {m.get("Company"): m.get("Allowed") for m in lib_b.get("LibraryMappings") or []}
    for company in sorted(set(maps_a) | set(maps_b), key=str):
        if maps_a.get(company) != maps_b.get(company):
            changes.append(f"company mapping {company}: {maps_a.get(company)!r} -> {maps_b.get(company)!r}")

    fns_a = {f.get("FunctionID"): f for f in lib_a.get("Functions") or []}
    fns_b = {f.get("FunctionID"): f for f in lib_b.get("Functions") or []}
    for function_id in sorted(set(fns_a) | set(fns_b), key=str):
        fa, fb = fns_a.get(function_id), fns_b.get(function_id)
        if fb is None:
            changes.append(f"function removed: {function_id}")
            continue
        if fa is None:
            changes.append(f"function added: {function_id}")
            continue
        for key in sorted((set(fa) | set(fb)) - NOISE_FUNCTION_FIELDS - {"Body", "FunctionSignature"}):
            if fa.get(key) != fb.get(key):
                changes.append(f"{function_id} {key}: {fa.get(key)!r} -> {fb.get(key)!r}")
        if (fa.get("FunctionSignature") or []) != (fb.get("FunctionSignature") or []):
            names_a = [p.get("ArgumentName") for p in fa.get("FunctionSignature") or []]
            names_b = [p.get("ArgumentName") for p in fb.get("FunctionSignature") or []]
            changes.append(f"{function_id} parameters: {names_a} -> {names_b}")
        code_a, code_b = _function_code(fa), _function_code(fb)
        for node in sorted(set(code_a) | set(code_b)):
            old, new = code_a.get(node, ""), code_b.get(node, "")
            if old.replace("\r\n", "\n") == new.replace("\r\n", "\n"):
                continue
            changes.append(
                f"{function_id}/{node} code: {len(old.splitlines())} -> {len(new.splitlines())} lines"
            )
            if show_code:
                changes.extend(
                    "    " + line.rstrip("\n")
                    for line in difflib.unified_diff(
                        old.splitlines(), new.splitlines(), "deployed", "planned", lineterm="", n=2
                    )
                )
        if fa.get("Body") != fb.get("Body") and code_a == code_b:
            changes.append(f"{function_id}: Body differs outside code nodes")
    return changes


def deployment_drift(deployment: Dict[str, Any], live_package: Dict[str, Any]) -> List[str]:
    """Differences between what the environment record last declared and
    what the target actually has now (changes made outside the pipeline)."""
    recorded = deployment.get("target") or {}
    live = live_package.get("Library") or {}
    drift = []
    for field in ("GlobalID", "OriginalID") + ENV_LIBRARY_FIELDS:
        if field not in recorded:
            continue
        a, b = recorded.get(field), live.get(field)
        if field == "LibraryMappings":
            a = sorted((m.get("Company"), m.get("Allowed")) for m in a or [])
            b = sorted((m.get("Company"), m.get("Allowed")) for m in b or [])
        if a != b:
            drift.append(f"{field}: recorded {recorded.get(field)!r}, live {live.get(field)!r}")
    return drift


def source_ref_accepted(source_repo: str, ref: str, accepted: List[str]) -> Optional[str]:
    """Return a branch matching one of the environment's acceptedSources
    patterns that contains ref, or None."""
    import fnmatch

    output = _git(source_repo, "branch", "-a", "--contains", ref, "--format=%(refname:short)").decode()
    branches = set()
    for line in output.splitlines():
        name = line.strip()
        if "/" in name and name.split("/", 1)[0] in {"origin", "remotes"}:
            name = name.split("/", 1)[1]
        if name and not name.endswith("HEAD"):
            branches.add(name)
    for branch in sorted(branches):
        if any(fnmatch.fnmatch(branch, pattern) for pattern in accepted):
            return branch
    return None


class KineticEfxLibraryService(KineticBaseClient):
    """Export/import/list Epicor EFx Function Libraries."""

    def _service_url(self, method_name: str, company: str = "") -> str:
        target_co = company or self.config["company"]
        return f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/{SERVICE_NAME}/{method_name}"

    def _call(
        self,
        method_name: str,
        payload: Optional[Dict[str, Any]] = None,
        company: str = "",
        http_method: str = "POST",
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        target_co = company or self.config["company"]
        return self.execute_request(
            http_method,
            self._service_url(method_name, company=target_co),
            payload=payload if http_method != "GET" else None,
            params=params,
            company=target_co,
        )

    def get_library_list(
        self,
        kind: str = "1",
        starts_with: str = "",
        roll_out_mode: str = "2",
        status: str = "2",
        company: str = "",
    ) -> List[Dict[str, Any]]:
        """List libraries visible to the current user.

        Live-tested against a test environment on 2026-10-07: Ice.Lib.EfxLibraryDesignerSvc
        /GetLibraryList2 with kind/startsWith/rollOutMode/status as strings,
        startsWith required (can be empty). export_all.py separately calls
        Ice.BO.EfxLibraryDesignerSvc/GetLibraryList with an int-wrapped
        "searchOptions" payload -- a different method name, not yet tested
        one way or the other here.
        """
        response = self._call(
            "GetLibraryList2",
            {
                "kind": str(kind),
                "startsWith": starts_with,
                "rollOutMode": str(roll_out_mode),
                "status": str(status),
            },
            company=company,
        )
        rows = (response.get("returnObj") or {}).get("EfxLibraryList") or []
        return [row for row in rows if isinstance(row, dict)]

    def get_libraries(self, library_ids: List[str], company: str = "") -> Dict[str, Any]:
        """Fetch the full tableset for one or more libraries: EfxLibrary,
        EfxFunction (with raw directive Body), EfxLibraryMapping (company
        assignments), EfxRefService, EfxRefTable, etc."""
        response = self._call("GetLibraries", {"libraryIds": list(library_ids)}, company=company)
        return response.get("returnObj") or {}

    def get_kinetic_function(self, library_id: str, function_id: str, company: str = "") -> Dict[str, Any]:
        """Fetch a single function's node graph. The service returns this
        as a JSON-encoded string, not an object -- parsed here for
        convenience."""
        response = self._call(
            "GetKineticFunction",
            http_method="GET",
            params={"libraryId": library_id, "functionId": function_id},
            company=company,
        )
        raw = response.get("returnObj")
        if isinstance(raw, str) and raw.strip():
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"_raw": raw}
        return raw or {}

    def extract_function_source(
        self,
        library_id: str,
        function_id: str,
        out_dir: str,
        company: str = "",
        tableset: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Pull one function's Body and split its embedded
        CustomCodeAction.Code fragments into individual .cs files under
        out_dir/<LibraryID>/<FunctionID>/, plus a _body.xml sidecar (the
        untouched original Body, used to reconstruct everything else about
        the function on push-back) and a _meta.json (row metadata with
        Body and volatile fields like SysRevID/SysRowID/BitFlag stripped,
        for human review/diffing only -- push-back always re-fetches fresh
        metadata rather than trusting this file).

        Only the XML Body wire format is handled here -- that's what
        GetLibraries returns live (verified against a test environment). The service
        also accepts a JSON wire format on write (seen in a captured
        ApplyChangesWithDiagnostics trace), but a live GetLibraries read
        returning that JSON form hasn't been observed, so round-tripping it
        isn't implemented.
        """
        if tableset is None:
            tableset = self.get_libraries([library_id], company=company)
        functions = tableset.get("EfxFunction") or []
        row = next(
            (f for f in functions if f.get("FunctionID") == function_id and f.get("LibraryID") == library_id),
            None,
        )
        if row is None:
            raise ValueError(f"Function '{function_id}' not found in library '{library_id}'.")

        body = row.get("Body") or ""
        safe_lib = _safe_path_component(library_id)
        safe_fn = _safe_path_component(function_id)
        func_dir = _resolve_contained_path(out_dir, safe_lib, safe_fn)
        os.makedirs(func_dir, exist_ok=True)

        written_files: List[str] = []
        node_provenance: List[Dict[str, str]] = []
        if body.lstrip().startswith("<"):
            root = ET.fromstring(body)
            used_names: Dict[str, int] = {}
            for index, (step, action) in enumerate(_iter_custom_code_actions(root)):
                code = action.get("Code") or ""
                node_id = (step.get("Id") if step is not None else "") or ""
                display_name = (step.get("DisplayName") if step is not None else "") or ""
                file_name = _node_file_name(display_name, index, used_names)
                file_path = _resolve_contained_path(func_dir, file_name)
                with open(file_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(code)
                written_files.append(file_path)
                node_provenance.append({"DisplayName": display_name, "NodeId": node_id, "File": file_name})

            body_sidecar = _resolve_contained_path(func_dir, "_body.xml")
            with open(body_sidecar, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        else:
            body_sidecar = _resolve_contained_path(func_dir, "_body.json")
            with open(body_sidecar, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)

        ignore_fields = DEFAULT_IGNORE_FIELDS | {"Body"}
        meta_rows = normalize_artifact_rows([row], ignore_fields=ignore_fields)
        meta_doc = meta_rows[0] if meta_rows else {}
        # NodeId is recorded here for provenance/debugging only -- it's the
        # GUID as seen in THIS environment at extraction time, and is never
        # read back as the source of truth for push-back (see
        # _node_file_name). A different environment may legitimately have a
        # different NodeId for the same logical (DisplayName-matched) node.
        meta_doc["Nodes"] = node_provenance
        meta_path = _resolve_contained_path(func_dir, "_meta.json")
        with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta_doc, f, indent=2, sort_keys=True)
            f.write("\n")

        return {
            "library_id": library_id,
            "function_id": function_id,
            "function_dir": func_dir,
            "code_files": written_files,
            "body_sidecar": body_sidecar,
            "meta_file": meta_path,
        }

    def export_library_source(self, library_id: str, out_dir: str, company: str = "") -> List[Dict[str, Any]]:
        """Extract every function in a library to
        out_dir/<LibraryID>/<FunctionID>/. One failing function doesn't
        abort the rest."""
        tableset = self.get_libraries([library_id], company=company)
        results: List[Dict[str, Any]] = []
        for row in tableset.get("EfxFunction") or []:
            function_id = row.get("FunctionID")
            if not function_id:
                continue
            try:
                result = self.extract_function_source(
                    library_id, function_id, out_dir, company=company, tableset=tableset
                )
                result["success"] = True
            except Exception as exc:
                result = {
                    "library_id": library_id,
                    "function_id": function_id,
                    "success": False,
                    "error": str(exc),
                }
            results.append(result)
        return results

    def push_function_source(
        self,
        library_id: str,
        function_id: str,
        out_dir: str,
        company: str = "",
        new_parameters: Optional[List[Dict[str, Any]]] = None,
        new_ref_tables: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Re-inject edited .cs file(s) from
        out_dir/<LibraryID>/<FunctionID>/ back into a freshly-fetched copy
        of the function's Body XML (not the locally cached _body.xml
        sidecar, so concurrent server-side changes to nodes this didn't
        touch aren't clobbered), then submit via ApplyChangesWithDiagnostics
        (ApplyChanges is deprecated in the live swagger spec; this uses the
        non-deprecated method). Raises if the server reports a blocking
        (error-level) diagnostic; warning-level diagnostics are returned
        in the result but don't block the save -- verified live: a push
        containing `ex.GetType().Name` succeeded and actually saved with
        diagnostics=["...warning ECF1002: The 'System.Reflection.MemberInfo.Name'
        property cannot be read."], confirming warnings don't roll back
        the change server-side.

        new_parameters optionally adds new input/output parameters in the
        same call. Each item is a dict: {"argument_name": str, "data_type":
        str (a .NET type name like "System.String"/"System.Int32"/
        "System.Boolean?"), "response": bool (False=input, True=output,
        default False), "optional": bool (default False), "default_value":
        Any (default None), "description": str (default "")}. ParameterID
        and Order are computed automatically, continuing from the highest
        existing value within the same (FunctionID, Response) group --
        verified live: a library function with 7 input
        rows (Response=false, ParameterID 1-7) and 1 output row
        (Response=true, ParameterID restarts at 1), confirming ParameterID
        is scoped per Response group, not global. New rows use RowMod="A"
        (the standard Epicor Added/Updated/Deleted dataset convention --
        "U" and "D" are directly verified live elsewhere in this module;
        "A" is what kinetic_devops/solutions.py already uses for new rows
        in its live, shipped `recreate` command, which is good corroborating
        evidence, but this method's own first live run is still the real
        verification for EfxFunctionSignature specifically).

        new_ref_tables optionally registers new ERP table references (e.g.
        "ERP.Vendor") the function's code now depends on. Without this, a
        table not already referenced compiles with CS1061 ("ILibraryContext'
        does not contain a definition for 'X'") -- confirmed live: a library
        that only had ERP.Company/ExtCompany/Part/PartCost/VendPart referenced,
        and adding Db.Vendor usage failed to compile until ERP.Vendor was
        added here. New rows are {"LibraryID", "TableID", "Updatable": False,
        "RowMod": "A"} -- Updatable is always False since this only adds
        read access; making a table writable wasn't needed or tested.

        metadata optionally overrides other EfxFunction row fields (e.g.
        {"Description": "..."}) in the same call -- merged onto the
        freshly-fetched row before submission, so it can't clobber a
        concurrent change to a field this didn't touch.

        Verified live: ApplyChangesWithDiagnostics returns
        {"parameters": {"libraryTableset": ..., "diagnostics": [...]}} --
        not the usual {"returnObj": ...} shape most other BO/Lib methods
        use. No Lock/Release/Regenerate call is made here: none appeared in
        the captured save trace this was built from, so adding one would be
        guessing rather than verifying.
        """
        safe_lib = _safe_path_component(library_id)
        safe_fn = _safe_path_component(function_id)
        func_dir = _resolve_contained_path(out_dir, safe_lib, safe_fn)

        tableset = self.get_libraries([library_id], company=company)
        functions = tableset.get("EfxFunction") or []
        row = next(
            (f for f in functions if f.get("FunctionID") == function_id and f.get("LibraryID") == library_id),
            None,
        )
        if row is None:
            raise ValueError(f"Function '{function_id}' not found in library '{library_id}'.")

        body = row.get("Body") or ""
        if not body.lstrip().startswith("<"):
            raise ValueError(
                f"Function '{function_id}' Body is not in the XML wire format; "
                "push-back for the JSON wire format isn't implemented."
            )
        root = ET.fromstring(body)

        changed = False
        new_body = body
        used_names: Dict[str, int] = {}
        for index, (step, action) in enumerate(_iter_custom_code_actions(root)):
            if step is None:
                continue
            node_id = step.get("Id") or ""
            display_name = step.get("DisplayName") or ""
            # Matched by DisplayName, not NodeId -- see _node_file_name for
            # why: the live NodeId (used just below, for the actual edit)
            # is always read fresh here, never assumed to match what a
            # local filename might have encoded from a different environment.
            file_name = _node_file_name(display_name, index, used_names)
            file_path = os.path.join(func_dir, file_name)
            if not os.path.isfile(file_path):
                continue
            with open(file_path, "r", encoding="utf-8") as f:
                new_code = f.read()
            if new_code != (action.get("Code") or ""):
                new_body = _replace_code_attribute_for_step(new_body, node_id, new_code)
                changed = True

        existing_sigs = [
            s
            for s in (tableset.get("EfxFunctionSignature") or [])
            if s.get("LibraryID") == library_id and s.get("FunctionID") == function_id
        ]
        new_sig_rows = _build_new_signature_rows(library_id, function_id, existing_sigs, new_parameters or [])

        existing_ref_tables = [r for r in (tableset.get("EfxRefTable") or []) if r.get("LibraryID") == library_id]
        new_ref_table_rows = _build_new_ref_table_rows(library_id, existing_ref_tables, new_ref_tables or [])

        metadata_changed = _has_metadata_changes(row, metadata or {})

        if not changed and not new_sig_rows and not new_ref_table_rows and not metadata_changed:
            return {"library_id": library_id, "function_id": function_id, "changed": False}

        function_rows = []
        if changed or metadata_changed:
            updated_row = dict(row)
            if changed:
                updated_row["Body"] = new_body
            if metadata_changed:
                updated_row.update(metadata)
            updated_row["RowMod"] = "U"
            function_rows.append(updated_row)

        tableset_payload = {
            "EfxLibrary": [],
            "EfxFunction": function_rows,
            "EfxFunctionSignature": new_sig_rows,
            "EfxLibraryMapping": [],
            "EfxRefAssembly": [],
            "EfxRefLibrary": [],
            "EfxRefService": [],
            "EfxRefTable": new_ref_table_rows,
        }
        response = self._call(
            "ApplyChangesWithDiagnostics",
            {"libraryTableset": tableset_payload},
            company=company,
        )
        result = response.get("parameters") or {}
        diagnostics = result.get("diagnostics") or []
        blocking = [d for d in diagnostics if _diagnostic_is_blocking(d)]
        if blocking:
            raise RuntimeError(f"ApplyChangesWithDiagnostics reported blocking diagnostics: {blocking}")

        return {
            "library_id": library_id,
            "function_id": function_id,
            "changed": True,
            "body_changed": changed,
            "metadata_changed": metadata_changed,
            "parameters_added": new_sig_rows,
            "ref_tables_added": new_ref_table_rows,
            "diagnostics": diagnostics,
        }

    def test_function_in_pilot(
        self,
        library_id: str,
        function_id: str,
        params: Optional[Dict[str, Any]] = None,
        company: str = "",
        allowed_env: str = "Pilot",
    ) -> Dict[str, Any]:
        """Actually EXECUTE a saved function via the EFx staging endpoint --
        this runs its real C# code, side effects included (the function
        this was reverse-engineered from calls Db.SaveChanges()). Hard
        restrictions:
          - Only runs if the resolved environment nickname matches
            allowed_env (default 'Pilot'). Any other environment, e.g. a
            post-deploy test in a test environment, must be named explicitly -- it is
            never inferred from the session.
          - Refuses to run unless both stdin and stdout are a real tty
            (never unattended/scripted/CI).
          - Requires typing the function id back as a confirmation prompt.

        Uses the staging endpoint for an unpublished library and the
        published endpoint (/api/v2/efx/{company}/{libraryId}/{functionId})
        once it's promoted.

        Verified live: POST {instance}/api/v2/efx/staging/{company}/{libraryId}/{functionId}
        with params as the JSON body actually executes the function with
        those parameter values and returns its output parameters. First
        seen in a captured trace of a zero-argument function (empty
        request body); now confirmed end-to-end against a real
        parameterized function (one with dryRun/logDetail
        inputs) -- the dry-run branch was honored (no live data was
        written) and updatedCount/unchangedCount/skippedCount/errorCount/
        summary/runLog all came back correctly in the response.
        """
        nickname = str(self.config.get("nickname") or "")
        if not allowed_env or nickname.lower() != allowed_env.lower():
            raise RuntimeError(
                f"Refusing to invoke a live EFx function: resolved environment '{nickname}' "
                f"is not the explicitly allowed environment '{allowed_env}'."
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError(
                "Refusing to invoke a live EFx function in a non-interactive session. "
                "This executes real code with real side effects and must be run attended."
            )

        print(f"\n⚠️  About to EXECUTE '{function_id}' in library '{library_id}' on {nickname}.")
        print(f"    This runs the function's actual C# code against real {nickname} data -- not a dry run.")
        confirmation = input(f"    Type the function id ('{function_id}') to confirm, or anything else to abort: ")
        if confirmation != function_id:
            raise RuntimeError("Confirmation did not match the function id; aborted.")

        target_co = company or self.config["company"]
        # The staging endpoint only serves unpublished libraries -- verified
        # live: it 404s for a published library (after a deploy), while
        # /api/v2/efx/{co}/{lib}/{fn} executed a published library's function
        # in a test environment. So pick the endpoint from the library's current state.
        libraries = self.get_libraries([library_id], company=target_co).get("EfxLibrary") or []
        published = bool(libraries and libraries[0].get("Published"))
        segment = "" if published else "staging/"
        url = f"{self.config['url'].rstrip('/')}/api/v2/efx/{segment}{target_co}/{library_id}/{function_id}"
        return self.execute_request("POST", url, payload=params or {}, company=target_co)

    def export_library(
        self,
        library_id: str,
        mode: int = 0,
        export_format: int = 1,
        package: Optional[str] = None,
        package_version: Optional[str] = None,
        publisher: Optional[str] = None,
        install_as_hidden: bool = False,
        company: str = "",
    ) -> str:
        """Export a library. Returns the raw base64 payload (not a zip --
        it decodes directly to XML/text); use export_library_to_file to
        decode and save it."""
        response = self._call(
            "ExportLibrary",
            {
                "libraryID": library_id,
                "options": {
                    "Mode": mode,
                    "Format": export_format,
                    "Package": package or library_id,
                    "PackageVersion": package_version,
                    "Publisher": publisher,
                    "InstallAsHidden": bool(install_as_hidden),
                },
            },
            company=company,
        )
        return response.get("returnObj") or ""

    def export_library_to_file(self, library_id: str, out_path: str, company: str = "", **export_kwargs) -> str:
        """Export a library and decode+save it to out_path. Returns the
        resolved output path (conflict-resolved via resolve_output_path)."""
        b64_data = self.export_library(library_id, company=company, **export_kwargs)
        if not b64_data:
            raise ValueError(f"No export payload returned for library '{library_id}'.")
        resolved = self.resolve_output_path(out_path)
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
        with open(resolved, "wb") as f:
            f.write(base64.b64decode(b64_data))
        return resolved

    def import_library(
        self,
        library_package_b64: str,
        new_library_id: Optional[str] = None,
        overwrite_mode: int = 0,
        company: str = "",
    ) -> Dict[str, Any]:
        """Import a library from the base64 payload produced by
        export_library/ExportLibrary. Returns the raw response (check
        BOUpdError for row-level failures -- a 200 doesn't guarantee a
        clean import)."""
        response = self._call(
            "ImportLibrary",
            {
                "libraryPackage": library_package_b64,
                "options": {
                    "NewLibraryId": new_library_id,
                    "OverwriteMode": int(overwrite_mode),
                },
            },
            company=company,
        )
        return response.get("returnObj") or {}

    def import_library_from_file(
        self,
        file_path: str,
        new_library_id: Optional[str] = None,
        overwrite_mode: int = 0,
        company: str = "",
    ) -> Dict[str, Any]:
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("ascii")
        return self.import_library(b64_data, new_library_id=new_library_id, overwrite_mode=overwrite_mode, company=company)

    def export_all_libraries(self, out_dir: str, company: str = "", skip_disabled: bool = True) -> List[Dict[str, Any]]:
        """Export every library visible to the current user, one file per
        library under out_dir/<LibraryID>/<LibraryID>.efxlib. Returns a
        per-library result list (success/error) instead of raising on the
        first failure, so one broken library doesn't abort the rest."""
        results: List[Dict[str, Any]] = []
        for lib in self.get_library_list(company=company):
            library_id = lib.get("LibraryID")
            if not library_id:
                continue
            if skip_disabled and lib.get("Disabled"):
                results.append({"library_id": library_id, "skipped": "disabled"})
                continue
            safe_id = _safe_path_component(library_id)
            try:
                out_path = _resolve_contained_path(out_dir, safe_id, f"{safe_id}.efxlib")
            except ValueError as exc:
                results.append({"library_id": library_id, "success": False, "error": str(exc)})
                continue
            try:
                # The real, unmodified library_id is still used for the API
                # call -- only the local output path is sanitized.
                resolved = self.export_library_to_file(library_id, out_path, company=company)
                results.append({"library_id": library_id, "output_file": resolved, "success": True})
            except Exception as exc:
                results.append({"library_id": library_id, "success": False, "error": str(exc)})
        return results

    def export_package(self, library_id: str, company: str = "") -> Dict[str, Any]:
        return decode_package(self.export_library(library_id, company=company))

    def import_package(self, package: Dict[str, Any], overwrite_mode: int = OVERWRITE_REPLACE, company: str = "") -> None:
        b64 = base64.b64encode(encode_package(package)).decode("ascii")
        result = self.import_library(b64, overwrite_mode=overwrite_mode, company=company)
        errors = result.get("BOUpdError") or []
        if errors:
            raise RuntimeError(f"ImportLibrary reported row errors: {errors}")

    def _environment_name(self) -> str:
        return str(self.config.get("nickname") or "")

    def plan_library_deployment(
        self,
        library_id: str,
        env_repo: str,
        source_repo: str,
        env_branch: str = "",
        show_code: bool = False,
        company: str = "",
    ) -> Dict[str, Any]:
        """Compare what env_branch declares for library_id with what the
        target actually has. Read-only. Returns the deployment record, the
        live and planned packages, changes, drift, policy result and notes."""
        env_name = self._environment_name()
        env_branch = env_branch or f"env/{env_name}"
        record_path = f"assets/libraries/{library_id}/deployment.json"
        raw = _git_show(env_repo, env_branch, record_path)
        if raw is None:
            raise ValueError(f"No {record_path} on {env_branch} in {env_repo}.")
        deployment = json.loads(raw.decode("utf-8-sig"))
        environment_raw = _git_show(env_repo, env_branch, "environment.json")
        environment = json.loads(environment_raw.decode("utf-8-sig")) if environment_raw else {}
        if environment.get("nickname") and environment["nickname"].lower() != env_name.lower():
            raise ValueError(
                f"{env_branch} describes environment '{environment['nickname']}', but the session is '{env_name}'."
            )
        target_id = (deployment.get("target") or {}).get("LibraryID") or library_id

        source_spec = deployment.get("source") or {}
        ref = source_spec.get("ref")
        if not ref:
            raise ValueError(f"{record_path} on {env_branch} has no source.ref.")
        source = read_library_source(source_repo, ref, source_spec.get("path") or library_id)
        accepted = environment.get("acceptedSources") or []
        accepted_branch = source_ref_accepted(source_repo, ref, accepted) if accepted else None

        live = self.export_package(target_id, company=company)
        planned, notes = build_package_from_target(live, source, deployment)
        return {
            "environment": env_name,
            "env_branch": env_branch,
            "library_id": target_id,
            "deployment": deployment,
            "source_ref": ref,
            "accepted_branch": accepted_branch,
            "accepted_sources": accepted,
            "live": live,
            "planned": planned,
            "changes": diff_packages(live, planned, show_code=show_code),
            "drift": deployment_drift(deployment, live),
            "notes": notes,
        }

    def deploy_library(
        self,
        library_id: str,
        env_repo: str,
        source_repo: str,
        env_branch: str = "",
        company: str = "",
    ) -> Dict[str, Any]:
        """Deploy what env_branch declares for library_id: plan, enforce the
        environment's acceptedSources, and if anything changes, snapshot the
        target to snapshot/<env>, import the planned package (OverwriteMode=1),
        re-export and verify it matches the plan, then record the new state
        on env_branch. Idempotent: a target that already matches is left
        untouched. Commits are local to env_repo; pushing them is separate."""
        from datetime import datetime

        plan = self.plan_library_deployment(library_id, env_repo, source_repo, env_branch, company=company)
        env_name, env_branch, target_id = plan["environment"], plan["env_branch"], plan["library_id"]
        if plan["accepted_sources"] and not plan["accepted_branch"]:
            raise RuntimeError(
                f"Source ref {plan['source_ref']} is not on any branch {env_name} accepts "
                f"({plan['accepted_sources']}); refusing to deploy."
            )
        if not plan["changes"]:
            return {**plan, "deployed": False}

        now = datetime.now().astimezone()
        stamp = now.strftime("%Y%m%d-%H%M%S")
        lib_dir = f"assets/libraries/{target_id}"
        snapshot_branch = f"snapshot/{env_name}"
        root = "env/root"
        planned_bytes = encode_package(plan["planned"])
        pre_path = f"{lib_dir}/{stamp}.pre-deploy.efxlib"
        _git_commit_files(
            env_repo, snapshot_branch, {pre_path: encode_package(plan["live"])},
            f"{target_id}: {env_name} pre-deploy export ({stamp})", start_point=root,
        )

        self.import_package(plan["planned"], company=company)
        after = self.export_package(target_id, company=company)
        mismatches = diff_packages(plan["planned"], after)
        _git_commit_files(
            env_repo, snapshot_branch,
            {f"{lib_dir}/{stamp}.deployed-package.efxlib": planned_bytes,
             f"{lib_dir}/{stamp}.post-deploy.efxlib": encode_package(after)},
            f"{target_id}: {env_name} deployed package and post-deploy export ({stamp})", start_point=root,
        )
        if mismatches:
            raise RuntimeError(
                f"Post-deploy export does not match the plan: {mismatches}. Not recorded on {env_branch}. "
                f"Rollback: import {snapshot_branch}:{pre_path} with OverwriteMode=1."
            )

        import hashlib

        deployment = dict(plan["deployment"])
        library = after["Library"]
        deployment["target"] = {
            "LibraryID": target_id,
            "GlobalID": library.get("GlobalID"),
            "OriginalID": library.get("OriginalID"),
            **{f: library.get(f) for f in ENV_LIBRARY_FIELDS},
        }
        deployment["target"]["LibraryMappings"] = sorted(
            library.get("LibraryMappings") or [], key=lambda m: str(m.get("Company"))
        )
        deployment["compatibility"] = {
            "Version": after.get("Version"),
            "EpicorVersion": library.get("EpicorVersion"),
            "override": (plan["deployment"].get("compatibility") or {}).get("override"),
        }
        deployment["lastDeployment"] = {
            "at": now.isoformat(timespec="seconds"),
            "method": "efx_library deploy: ImportLibrary OverwriteMode=1, package built on the target's export",
            "packageSha256": hashlib.sha256(planned_bytes).hexdigest(),
            "preDeploySnapshot": f"{snapshot_branch}:{pre_path}",
            "changes": plan["changes"],
            "verification": "post-deploy export matches the planned package",
            "gate": {"result": "not-run"},
        }
        record = (json.dumps(deployment, indent=2) + "\n").encode("utf-8")
        _git_commit_files(
            env_repo, env_branch, {f"{lib_dir}/deployment.json": record},
            f"{target_id}: deployed source {plan['source_ref'][:7]} to {env_name}", start_point=root,
        )
        return {**plan, "deployed": True, "after": after, "pre_deploy_snapshot": f"{snapshot_branch}:{pre_path}"}


def _print_plan(plan: Dict[str, Any]) -> None:
    print(f"== {plan['library_id']} on {plan['environment']} (declared by {plan['env_branch']})")
    print(f"   source ref {plan['source_ref']} "
          f"accepted via: {plan['accepted_branch'] or ('NOT ACCEPTED' if plan['accepted_sources'] else 'no policy')}")
    for note in plan["notes"]:
        print(f"   note: {note}")
    if plan["drift"]:
        print("   drift since last recorded deployment:")
        for line in plan["drift"]:
            print(f"     ~ {line}")
    if plan["changes"]:
        print(f"   {len(plan['changes'])} change(s) to deploy:")
        for line in plan["changes"]:
            print(f"     {line}" if line.startswith("    ") else f"     + {line}")
    else:
        print("   no changes: target already matches what is declared")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage Epicor Kinetic EFx function libraries (Ice.Lib.EfxLibraryDesignerSvc): list, export and "
        "import whole libraries, pull/push individual functions' C# source, run a function, and plan/deploy a "
        "library from source plus an environment repo.",
    )
    parser.add_argument("-e", "--env", metavar="NICKNAME", help="Environment nickname to connect to (from `auth store`).")
    parser.add_argument("-u", "--user", metavar="USER", help="User whose stored session to use (or set KIN_USER).")
    parser.add_argument("--company", metavar="COMPANY", help="Company to act in (default: the session's current company).")
    KineticBaseClient.add_file_resolution_args(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    subparsers.add_parser(
        "list", help="List visible libraries",
        description="Read-only. Prints each library visible to the user: ID, enabled/disabled, description.",
    )

    export_p = subparsers.add_parser(
        "export", help="Export a single library to a package file",
        description="Read-only on the server. Saves the library's ExportLibrary package (JSON, UTF-8 with BOM) to --out. "
        "If --out already exists, --file-conflict decides what happens (default: write a timestamped name instead).",
    )
    export_p.add_argument("library_id", help="Library ID to export")
    export_p.add_argument("--out", required=True, metavar="FILE", help="Output package path (e.g. MyLibrary.efxlib)")

    export_all_p = subparsers.add_parser(
        "export-all", help="Export every visible library into out_dir/<LibraryID>/",
        description="Read-only on the server. Exports each visible, enabled library to <out-dir>/<LibraryID>/"
        "<LibraryID>.efxlib. One failing library doesn't stop the rest; exits 1 if any failed.",
    )
    export_all_p.add_argument("--out-dir", required=True, metavar="DIR", help="Directory to write the packages into")

    import_p = subparsers.add_parser(
        "import", help="Import a library package (writes to Epicor)",
        description="WRITES TO EPICOR. Imports a package file via ImportLibrary. With --overwrite-mode 0 the import "
        "fails if the library already exists; 1 replaces the existing library completely, including removing "
        "parameters and references the package doesn't contain. The package's Published flag is applied as-is.",
    )
    import_p.add_argument("file_path", help="Package file to import (as written by `export`; must keep its UTF-8 BOM)")
    import_p.add_argument("--new-library-id", metavar="ID", help="Import under this library ID instead of the package's")
    import_p.add_argument(
        "--overwrite-mode", type=int, default=0, metavar="N",
        help="0 = refuse if the library exists (default); 1 = replace the existing library in full",
    )

    pull_source_p = subparsers.add_parser(
        "pull-source", help="Extract a library's functions into editable .cs files under out_dir/<LibraryID>/<FunctionID>/",
        description="Read-only on the server. For each function writes one .cs file per custom-code node (named from "
        "the node's DisplayName), _body.xml (the original Body) and _meta.json (function metadata, volatile fields "
        "removed). Existing files are overwritten.",
    )
    pull_source_p.add_argument("library_id", help="Library ID to pull")
    pull_source_p.add_argument("--out-dir", required=True, metavar="DIR", help="Root directory for the extracted source")

    push_source_p = subparsers.add_parser(
        "push-source", help="Push edited .cs file(s) for one function (writes to Epicor)",
        description="WRITES TO EPICOR. Re-injects the function's .cs files into a freshly fetched copy of its Body "
        "and saves it with ApplyChangesWithDiagnostics, optionally adding parameters and table references. Fails "
        "on error-level diagnostics; warnings are printed. Prints 'No changes' and writes nothing if nothing "
        "differs. Only adds -- it never removes parameters or references.",
    )
    push_source_p.add_argument("library_id", help="Library ID")
    push_source_p.add_argument("function_id", help="Function ID")
    push_source_p.add_argument(
        "--out-dir", required=True, metavar="DIR",
        help="Root directory used by pull-source (files are read from <DIR>/<LibraryID>/<FunctionID>/)",
    )
    push_source_p.add_argument(
        "--add-param",
        action="append",
        default=[],
        metavar="name:dataType[:response][:optional]",
        help=(
            "Add a new input/output parameter, may be repeated. dataType is a .NET type "
            "name (System.String, System.Int32, System.Boolean?, ...). Add the literal "
            "word 'response' to mark it an output parameter, 'optional' to mark it optional. "
            "Example: --add-param dryRun:System.Boolean --add-param updatedCount:System.Int32:response"
        ),
    )
    push_source_p.add_argument(
        "--add-ref-table",
        action="append",
        default=[],
        metavar="ERP.TableName",
        help="Register a new ERP table reference the code now depends on, may be repeated (e.g. --add-ref-table ERP.Vendor)",
    )

    test_pilot_p = subparsers.add_parser(
        "test-pilot",
        help="Execute a saved function for real (interactive confirmation required)",
        description="RUNS THE FUNCTION'S REAL CODE, side effects included. Calls the staging endpoint for an "
        "unpublished library or the published endpoint once it's promoted, and prints the output parameters. "
        "Only runs in the environment named by --allow-env, from an interactive terminal, after you type the "
        "function ID back.",
    )
    test_pilot_p.add_argument("library_id", help="Library ID")
    test_pilot_p.add_argument("function_id", help="Function ID")
    test_pilot_p.add_argument(
        "--params", help="JSON object of parameters to pass (avoid on PowerShell -- shell quoting of "
        "embedded double-quotes to a native exe is unreliable there; use --params-file instead)",
        default=None,
    )
    test_pilot_p.add_argument(
        "--params-file", help="Path to a JSON file containing the parameters object (sidesteps shell quoting entirely)",
        default=None,
    )
    test_pilot_p.add_argument(
        "--allow-env",
        default="Pilot",
        metavar="NICKNAME",
        help="Environment this run may execute in (default: %(default)s). Must match the resolved -e environment, "
        "e.g. -e <env> --allow-env <env> for a post-deploy test.",
    )

    deploy_common = argparse.ArgumentParser(add_help=False)
    deploy_common.add_argument("library_id", help="Library ID as named in the environment repo (assets/libraries/<ID>/)")
    deploy_common.add_argument(
        "--env-repo", required=True, metavar="DIR",
        help="Local clone of the environment repo (e.g. projects/env). Read from git refs, not the working tree.",
    )
    deploy_common.add_argument(
        "--source-repo", required=True, metavar="DIR",
        help="Local clone of the library's source repo; the declared source ref must already be fetched into it.",
    )
    deploy_common.add_argument(
        "--env-branch", default="", metavar="BRANCH",
        help="Environment branch declaring the desired state (default: env/<resolved -e nickname>).",
    )

    plan_p = subparsers.add_parser(
        "plan", parents=[deploy_common],
        help="Show what deploy would change (read-only)",
        description="Read-only. Builds the package the environment branch declares (source code and metadata at the "
        "declared ref, per-environment settings from deployment.json, IDs from the live target) and lists how it "
        "differs from what is deployed, plus any drift from the last recorded deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  efx_library -e <env> plan MyLibrary --env-repo ../env --source-repo ../MyLibrarySource",
    )
    plan_p.add_argument("--show-code", action="store_true", help="Include a unified diff of each changed code node")

    deploy_p = subparsers.add_parser(
        "deploy", parents=[deploy_common],
        help="Make the target match its environment branch (writes to Epicor)",
        description="Plans, then -- only if something differs and the source ref is on a branch the environment "
        "accepts (environment.json acceptedSources) -- snapshots the live library to snapshot/<env>, imports the "
        "planned package with OverwriteMode=1, re-exports to verify it matches, and records the result on the "
        "environment branch. A target that already matches is left untouched. Environment-repo commits are local; "
        "push them separately. Rollback: point deployment.json at an earlier source ref and deploy again, or "
        "import the pre-deploy snapshot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n  efx_library -e <env> deploy MyLibrary --env-repo ../env --source-repo ../MyLibrarySource",
    )
    deploy_p.add_argument(
        "--yes", action="store_true",
        help="Skip the interactive confirmation (for pipelines). Without it, deploy requires a terminal and the "
        "environment name typed back.",
    )

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    service = KineticEfxLibraryService(args.env, args.user, company_id=args.company)
    service.configure_file_resolution_from_args(args)

    try:
        if args.command == "list":
            for lib in service.get_library_list():
                print(f"{lib.get('LibraryID')}\t{'disabled' if lib.get('Disabled') else 'enabled'}\t{lib.get('Description') or ''}")
        elif args.command == "export":
            resolved = service.export_library_to_file(args.library_id, args.out)
            print(f"✅ Exported {args.library_id} -> {resolved}")
        elif args.command == "export-all":
            results = service.export_all_libraries(args.out_dir)
            ok = sum(1 for r in results if r.get("success"))
            failed = [r for r in results if not r.get("success") and not r.get("skipped")]
            print(f"✅ Exported {ok}/{len(results)} libraries to {args.out_dir}")
            for r in failed:
                print(f"  ❌ {r['library_id']}: {r.get('error')}")
            if failed:
                sys.exit(1)
        elif args.command == "import":
            result = service.import_library_from_file(args.file_path, new_library_id=args.new_library_id, overwrite_mode=args.overwrite_mode)
            errors = result.get("BOUpdError") or []
            if errors:
                print(f"⚠️  Import completed with {len(errors)} error(s): {errors}")
                sys.exit(1)
            else:
                print("✅ Import completed with no errors.")
        elif args.command == "pull-source":
            results = service.export_library_source(args.library_id, args.out_dir)
            ok = sum(1 for r in results if r.get("success"))
            failed = [r for r in results if not r.get("success")]
            print(f"✅ Pulled source for {ok}/{len(results)} function(s) to {args.out_dir}")
            for r in failed:
                print(f"  ❌ {r['function_id']}: {r.get('error')}")
            if failed:
                sys.exit(1)
        elif args.command == "push-source":
            new_parameters = []
            for spec in args.add_param:
                parts = spec.split(":")
                if len(parts) < 2:
                    raise ValueError(f"Invalid --add-param '{spec}': expected name:dataType[:response][:optional]")
                name, data_type, flags = parts[0], parts[1], {p.lower() for p in parts[2:]}
                new_parameters.append(
                    {
                        "argument_name": name,
                        "data_type": data_type,
                        "response": "response" in flags,
                        "optional": "optional" in flags,
                    }
                )
            result = service.push_function_source(
                args.library_id, args.function_id, args.out_dir,
                new_parameters=new_parameters, new_ref_tables=args.add_ref_table,
            )
            if not result.get("changed"):
                print("= No changes detected; nothing pushed.")
            else:
                print(
                    f"✅ Pushed changes to {args.function_id} "
                    f"(body_changed={result.get('body_changed')}, "
                    f"parameters_added={len(result.get('parameters_added') or [])}, "
                    f"ref_tables_added={len(result.get('ref_tables_added') or [])}); "
                    f"diagnostics: {result.get('diagnostics')}"
                )
        elif args.command == "test-pilot":
            if args.params and args.params_file:
                raise ValueError("Use either --params or --params-file, not both.")
            if args.params_file:
                # utf-8-sig tolerates a UTF-8 BOM, which PowerShell's
                # Out-File/Set-Content -Encoding utf8 writes by default --
                # verified live that a BOM-written file breaks plain "utf-8".
                with open(args.params_file, "r", encoding="utf-8-sig") as f:
                    params = json.load(f)
            else:
                params = json.loads(args.params) if args.params else {}
            result = service.test_function_in_pilot(
                args.library_id, args.function_id, params=params, allowed_env=args.allow_env
            )
            print(f"✅ Executed. Response: {result}")
        elif args.command in ("plan", "deploy"):
            plan = service.plan_library_deployment(
                args.library_id, args.env_repo, args.source_repo, args.env_branch,
                show_code=getattr(args, "show_code", False),
            )
            _print_plan(plan)
            if args.command == "deploy" and plan["changes"]:
                if not args.yes:
                    if not sys.stdin.isatty():
                        raise RuntimeError("deploy needs --yes when not run from an interactive terminal.")
                    env_name = plan["environment"]
                    if input(f"Type '{env_name}' to deploy these changes: ") != env_name:
                        raise RuntimeError("Confirmation did not match; aborted.")
                result = service.deploy_library(args.library_id, args.env_repo, args.source_repo, args.env_branch)
                if result["deployed"]:
                    print(f"✅ Deployed and verified. Pre-deploy snapshot: {result['pre_deploy_snapshot']}")
                    print(f"   Recorded on {result['env_branch']} (local) -- push the environment repo to publish it.")
                else:
                    print("= Nothing to deploy (target changed to match between plan and deploy).")
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
