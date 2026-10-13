#!/usr/bin/env python3
"""
scripts/json_helper.py

Utilities to extract stringified JSON from a wrapper file into a clean .content.jsonc file,
and inject it back (stringify) into the original file.

Usage:
  python scripts/json_helper.py extract MyLayout.jsonc
  python scripts/json_helper.py inject MyLayout.jsonc
  python scripts/json_helper.py build MyLayer.config.jsonc
    python scripts/json_helper.py build MyLayer.jsonc --deploy --env <ENV> --user <USER>
"""
import json
import argparse
import sys
import os
import re
import zipfile
import base64
import requests

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from kinetic_devops.auth import KineticConfigManager

# Common keys used in Kinetic/Epicor for stringified payloads
TARGET_KEYS = ['Content', 'Value', 'Body', 'Personalization', 'SysCharacter03']

def stringify_json(value):
    """Stringify JSON payload for embedding in layer fields."""
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

def derive_layer_zip_name(layer_data, fallback_stem):
    """Build a readable zip name from layer metadata when available."""
    if not isinstance(layer_data, dict):
        return f"{fallback_stem}.zip"

    app_key = str(layer_data.get('Key2', '')).strip()
    version = str(layer_data.get('Version', '')).strip()
    version_date = version[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', version) else ''

    parts = [p for p in [app_key, fallback_stem, version_date] if p]
    if parts:
        return f"{' '.join(parts)}.zip"
    return f"{fallback_stem}.zip"

def unique_output_path(path):
    """Avoid overwriting existing files by adding a numeric suffix."""
    if not os.path.exists(path):
        return path

    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1

def package_layer_zip(layer_path, layer_data):
    """Package a layer jsonc into upload-ready Layers/<App>/<Layer>.jsonc zip."""
    layer_filename = os.path.basename(layer_path)
    fallback_stem = os.path.splitext(layer_filename)[0]

    app_key = str(layer_data.get('Key2', '')).strip() if isinstance(layer_data, dict) else ''
    if not app_key:
        app_key = os.path.basename(os.path.dirname(layer_path))

    arcname = f"Layers/{app_key}/{layer_filename}"

    customizations_dir = os.path.join(os.getcwd(), 'projects', 'Customizations')
    output_dir = customizations_dir if os.path.isdir(customizations_dir) else os.path.dirname(layer_path)
    zip_name = derive_layer_zip_name(layer_data, fallback_stem)
    zip_path = unique_output_path(os.path.join(output_dir, zip_name))

    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(layer_path, arcname=arcname)

    print(f"✅ Packaged upload zip: {zip_path}")
    print(f"   ↳ {arcname}")
    return zip_path

def build_runtime_substitutions(url, company, user_id, plant=""):
    parsed = re.match(r'^https?://([^/]+)/(.*)$', url.rstrip('/'))
    hostname = parsed.group(1) if parsed else ''
    path = parsed.group(2) if parsed else ''

    return {
        'HOSTNAME': hostname,
        'hostname': hostname,
        'INSTANCE': path,
        'COMPANY': company,
        'Company': company,
        'COMPANYID': company,
        'USER_ID': user_id,
        'USERID': user_id,
        'PLANT': plant,
    }

def substitute_placeholders(value, mapping):
    if isinstance(value, str):
        return re.sub(r'\{([A-Za-z0-9_]+)\}', lambda m: mapping.get(m.group(1), m.group(0)), value)
    if isinstance(value, dict):
        return {k: substitute_placeholders(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_placeholders(v, mapping) for v in value]
    return value

def resolve_active_config(env=None, user=None, company=None):
    mgr = KineticConfigManager()

    if env:
        context = (env, user, company)
    else:
        env_name, user_id, company_id = mgr.prompt_for_env()
        if user:
            user_id = user
        if company:
            company_id = company
        context = (env_name, user_id, company_id)

    url, token, api_key, active_company, nickname, active_user = mgr.get_active_config(
        context,
        fields=('url', 'token', 'api_key', 'company', 'nickname', 'user_id')
    )

    if not url or not token or not api_key or not active_company:
        print('❌ Could not resolve active Kinetic config/token for deploy.')
        sys.exit(1)

    return mgr, {
        'url': url,
        'token': token,
        'api_key': api_key,
        'company': company or active_company,
        'nickname': nickname,
        'user_id': active_user,
    }

def _split_companies(raw):
    return [c.strip() for c in str(raw or '').split(',') if c.strip()]

def _candidate_delete_companies(mgr, config, layer_data, tenant_company=''):
    # Prefer explicit layer company when present, then active company, then all configured companies.
    candidates = []
    explicit_tenant = str(tenant_company or '').strip()
    if explicit_tenant and explicit_tenant.lower() != 'all companies':
        candidates.append(explicit_tenant)

    explicit_layer_company = str(layer_data.get('Company', '')).strip() if isinstance(layer_data, dict) else ''
    if explicit_layer_company and explicit_layer_company.lower() != 'all companies':
        candidates.append(explicit_layer_company)

    active_company = str(config.get('company', '')).strip()
    if active_company:
        candidates.append(active_company)

    base_cfg = mgr.get_base_config(config.get('nickname', '')) or {}
    for co in _split_companies(base_cfg.get('companies')):
        candidates.append(co)

    ordered = []
    seen = set()
    for co in candidates:
        key = co.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(co)
    return ordered

def _find_layer_owner_companies(base_url, headers, app_id, layer_name, type_code, device_type, timeout):
    """Query MetaFX layer list and return matching owner companies for this layer."""
    list_url = f"{base_url}/api/v2/odata/{headers.get('X-Epicor-Company', '')}/Ice.LIB.MetaFXSvc/GetApplications"
    req = {
        'request': {
            'Type': 'view',
            'SubType': '',
            'SearchText': app_id,
            'IncludeAllLayers': True,
            'IncludePersLayers': False,
        }
    }
    resp = requests.post(list_url, json=req, headers=headers, timeout=timeout)
    if not resp.ok:
        return []

    owners = []
    try:
        payload = resp.json() or {}
        rows = payload.get('returnObj') or []
        for row in rows:
            if str(row.get('Id', '')).strip() != app_id:
                continue
            layers = row.get('Layers') or []
            for layer in layers:
                if str(layer.get('LayerName', '')).strip() != layer_name:
                    continue
                if str(layer.get('TypeCode', '')).strip() != type_code:
                    continue
                this_device = str(layer.get('DeviceType', 'Desktop')).strip() or 'Desktop'
                if this_device != device_type:
                    continue
                co = str(layer.get('Company', '')).strip()
                if co and co not in owners:
                    owners.append(co)
    except Exception:
        return []

    return owners

def _delete_layer_for_company(base_url, headers, app_id, layer_name, device_type, type_code, layer_data, company_id, substitutions, timeout, plant=''):
    delete_url = f"{base_url}/api/v2/odata/{company_id}/Erp.BO.ErpMetaFxSvc/BulkDeleteLayers"
    delete_payload = {
        'layersToDelete': [
            {
                'Id': app_id,
                'SubType': 'Apps',
                'LastUpdated': str(layer_data.get('LastUpdated', '')).strip(),
                'IsPublished': True,
                'IsSilentExport': False,
                'TypeCode': type_code,
                'Company': company_id,
                'LayerName': layer_name,
                'DeviceType': device_type,
                'CGCCode': str(layer_data.get('CGCCode', '')).strip(),
                'SystemFlag': bool(layer_data.get('SystemFlag', False)),
                'HasDraftContent': bool(layer_data.get('HasDraftContent', False)),
                'LastUpdatedBy': str(layer_data.get('LastUpdatedBy', '')).strip(),
                'Type': 'view',
            }
        ]
    }
    last_updated = str(delete_payload['layersToDelete'][0].get('LastUpdated', '')).strip()
    if last_updated and not last_updated.endswith('Z'):
        delete_payload['layersToDelete'][0]['LastUpdated'] = f"{last_updated}Z"

    delete_payload = substitute_placeholders(delete_payload, substitutions)
    delete_headers = dict(headers)
    delete_headers['X-Epicor-Company'] = company_id
    if plant:
        delete_headers['callSettings'] = json.dumps({'Company': company_id, 'Plant': plant})

    return requests.post(delete_url, json=delete_payload, headers=delete_headers, timeout=timeout)

def deploy_layer(
    zip_path,
    layer_data,
    env=None,
    user=None,
    company=None,
    tenant_company=None,
    plant='',
    timeout=120,
    skip_delete=False,
    skip_import=False,
):
    if not zip_path or not os.path.exists(zip_path):
        print(f'❌ Deploy failed: zip not found: {zip_path}')
        sys.exit(1)

    if not isinstance(layer_data, dict):
        print('❌ Deploy failed: layer metadata object is required.')
        sys.exit(1)

    mgr, config = resolve_active_config(env=env, user=user, company=company)
    substitutions = build_runtime_substitutions(config['url'], config['company'], config['user_id'], plant=plant)

    base_url = config['url'].rstrip('/')
    headers = mgr.get_auth_headers(config)
    headers.update({
        'Content-Type': 'application/json; charset=utf-8',
        'Accept': 'application/json, text/plain, */*',
        'x-epi-request-etag': 'true',
        'x-epi-extension-serialization': 'full-metadata',
    })
    if plant:
        headers['callSettings'] = json.dumps({'Company': config['company'], 'Plant': plant})

    app_id = str(layer_data.get('Key2', '')).strip()
    layer_name = str(layer_data.get('Key1', '')).strip()
    device_type = str(layer_data.get('Key3', 'Desktop')).strip() or 'Desktop'
    type_code = str(layer_data.get('TypeCode', 'KNTCCustLayer')).strip() or 'KNTCCustLayer'
    target_tenant_company = str(tenant_company or layer_data.get('Company', '')).strip()
    delete_companies = _candidate_delete_companies(mgr, config, layer_data, tenant_company=target_tenant_company)

    print(f"ℹ️ Working company context: {config['company']}")
    if target_tenant_company:
        print(f"ℹ️ Target tenant company: {target_tenant_company}")

    owner_companies = _find_layer_owner_companies(
        base_url=base_url,
        headers=headers,
        app_id=app_id,
        layer_name=layer_name,
        type_code=type_code,
        device_type=device_type,
        timeout=timeout,
    )
    if owner_companies:
        merged = owner_companies + delete_companies
        deduped = []
        seen = set()
        for co in merged:
            k = co.lower()
            if k in seen:
                continue
            seen.add(k)
            deduped.append(co)
        delete_companies = deduped
        print(f"ℹ️ Layer owner company discovered from GetApplications: {', '.join(owner_companies)}")

    if not app_id or not layer_name:
        print('❌ Deploy failed: layer must include Key2 (app id) and Key1 (layer name).')
        sys.exit(1)

    if not skip_delete:
        print(f"🚀 Deleting existing layer (if present): {app_id} / {layer_name}")
        deleted_in_company = None
        last_delete_error = ''
        for candidate_company in delete_companies:
            print(f"   ↳ checking company: {candidate_company}")
            delete_resp = _delete_layer_for_company(
                base_url=base_url,
                headers=headers,
                app_id=app_id,
                layer_name=layer_name,
                device_type=device_type,
                type_code=type_code,
                layer_data=layer_data,
                company_id=candidate_company,
                substitutions=substitutions,
                timeout=timeout,
                plant=plant,
            )
            if delete_resp.ok:
                deleted_in_company = candidate_company
                print(f"✅ Delete completed in company: {candidate_company}")
                break
            last_delete_error = (delete_resp.text or '').strip()

        if not deleted_in_company:
            print(f"❌ Delete failed in all candidate companies ({', '.join(delete_companies)}).")
            print(f"   Last error: {last_delete_error}")
            sys.exit(1)

    if skip_import:
        print('✅ Delete-only operation complete (import skipped).')
        return

    import_url = f"{base_url}/api/v2/odata/{config['company']}/Ice.LIB.MetaFXSvc/ImportLayers"
    with open(zip_path, 'rb') as f:
        zip_b64 = base64.b64encode(f.read()).decode('utf-8')

    import_payload = {
        'fileContent': {
            'content': zip_b64,
            'overwrite': True,
            'overrideCompanyId': 'All Companies',
            'overridePersonalizationCompanyId': target_tenant_company or config['company'],
            'overrideCustomizationUserId': config['user_id'],
            'overridePersonalizationUserId': config['user_id'],
        }
    }
    import_payload = substitute_placeholders(import_payload, substitutions)

    print(f"🚀 Importing layer zip: {zip_path}")
    import_resp = requests.post(import_url, json=import_payload, headers=headers, timeout=timeout)
    if not import_resp.ok:
        body = (import_resp.text or '').strip()
        lower_body = body.lower()
        if (not skip_delete) and ('already exists in another company' in lower_body):
            print('⚠️ Import conflict indicates layer exists in another company. Running cross-company cleanup and retrying once...')
            for candidate_company in delete_companies:
                print(f"   ↳ cleanup delete in: {candidate_company}")
                _delete_layer_for_company(
                    base_url=base_url,
                    headers=headers,
                    app_id=app_id,
                    layer_name=layer_name,
                    device_type=device_type,
                    type_code=type_code,
                    layer_data=layer_data,
                    company_id=candidate_company,
                    substitutions=substitutions,
                    timeout=timeout,
                    plant=plant,
                )
            import_resp = requests.post(import_url, json=import_payload, headers=headers, timeout=timeout)
            if not import_resp.ok:
                body = (import_resp.text or '').strip()
                print(f"❌ Import failed after cleanup retry: {import_resp.status_code} {body}")
                sys.exit(1)
        else:
            print(f"❌ Import failed: {import_resp.status_code} {body}")
            sys.exit(1)

    print('✅ Deploy complete: layer imported successfully.')

def load_jsonc(path):
    """Helper to load JSONC files."""
    with open(path, 'r', encoding='utf-8') as f:
        return json.loads(strip_comments(f.read()))

def strip_comments(content):
    """Removes // and /* */ comments from JSONC."""
    content = re.sub(r'//.*', '', content)
    content = re.sub(r'/\*[\s\S]*?\*/', '', content)
    return content

def find_and_extract(data):
    """
    Recursively searches for a likely stringified JSON field.
    Returns (key, parsed_value) if found, else (None, None).
    """
    if isinstance(data, dict):
        # Priority search for known keys
        for key in TARGET_KEYS:
            if key in data and isinstance(data[key], str):
                val = data[key].strip()
                if (val.startswith('{') and val.endswith('}')) or (val.startswith('[') and val.endswith(']')):
                    try:
                        return key, json.loads(val)
                    except json.JSONDecodeError:
                        pass
        
        # Fallback: search children
        for k, v in data.items():
            found_k, found_v = find_and_extract(v)
            if found_k: return found_k, found_v
    
    elif isinstance(data, list):
        for item in data:
            found_k, found_v = find_and_extract(item)
            if found_k: return found_k, found_v
            
    return None, None

def inject_content(data, content_str, target_key_hint=None):
    """
    Recursively finds the target key and replaces it with content_str.
    Returns True if replaced.
    """
    if isinstance(data, dict):
        for key in ([target_key_hint] if target_key_hint else TARGET_KEYS):
            if key in data:
                # We assume if the key exists, it's the target. 
                # Validating it matches the old structure is hard if the content changed completely.
                data[key] = content_str
                return True
        
        for k, v in data.items():
            if inject_content(v, content_str, target_key_hint):
                return True
    elif isinstance(data, list):
        for item in data:
            if inject_content(item, content_str, target_key_hint):
                return True
    return False

def build_layer(
    config_path,
    deploy=False,
    env=None,
    user=None,
    company=None,
    tenant_company=None,
    plant='',
    timeout=120,
    skip_delete=False,
    delete_only=False,
    import_only=False,
):
    """
    Builds a Kinetic artifact.

    Supported inputs:
    1) Config mode: a JSONC file with keys like `template`, `content`, `output`
    2) Direct mode: a wrapper/layer file (e.g., MyLayer.jsonc) with sibling
       content file (e.g., MyLayer.content.jsonc)
    """
    base_dir = os.path.dirname(config_path)
    
    # 1. Load Config
    try:
        config = load_jsonc(config_path)
    except Exception as e:
        print(f"❌ Error loading config {config_path}: {e}")
        sys.exit(1)

    def resolve(p):
        return os.path.join(base_dir, p) if not os.path.isabs(p) else p

    # Config mode: existing behavior
    if isinstance(config, dict) and 'template' in config:
        # 2. Load Template (The wrapper)
        template_rel = config.get('template')
        if not template_rel:
            print("❌ Config missing 'template' path.")
            sys.exit(1)
            
        template_path = resolve(template_rel)
        if not os.path.exists(template_path):
            print(f"❌ Template file not found: {template_path}")
            sys.exit(1)
            
        container_data = load_jsonc(template_path)

        # 3. Inject Content
        content_def = config.get('content')
        if content_def:
            if not isinstance(content_def, dict) or 'source' not in content_def:
                print("❌ Config 'content' must be an object with a 'source' property.")
                sys.exit(1)

            source_path = resolve(content_def['source'])
            target_key = content_def.get('key') # Optional specific key

            if os.path.exists(source_path):
                content_obj = load_jsonc(source_path)
                # Re-stringify the clean content
                content_str = stringify_json(content_obj)
                
                if not inject_content(container_data, content_str, target_key):
                    print(f"⚠️  Warning: Could not find target key to inject content into {template_rel}")
            else:
                print(f"❌ Content source file not found: {source_path}")
                sys.exit(1)

        # 4. Save Output
        output_rel = config.get('output', 'dist/Artifact.jsonc')
        output_path = resolve(output_rel)
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(container_data, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Built artifact: {output_path}")

        zip_path = None
        if isinstance(container_data, dict) and output_path.lower().endswith('.jsonc'):
            zip_path = package_layer_zip(output_path, container_data)
            if deploy:
                deploy_layer(
                    zip_path=zip_path,
                    layer_data=container_data,
                    env=env,
                    user=user,
                    company=company,
                    tenant_company=tenant_company,
                    plant=plant,
                    timeout=timeout,
                    skip_delete=skip_delete or import_only,
                    skip_import=delete_only,
                )
        return

    # Direct mode: treat input as wrapper file and inject sibling .content.jsonc
    if not isinstance(config, (dict, list)):
        print("❌ Build input must be a JSON object/array (wrapper) or a config object.")
        sys.exit(1)

    container_data = config
    base, _ = os.path.splitext(config_path)
    source_path = f"{base}.content.jsonc"

    if not os.path.exists(source_path):
        print(f"❌ Content source file not found: {source_path}")
        sys.exit(1)

    content_obj = load_jsonc(source_path)
    content_str = stringify_json(content_obj)

    if not inject_content(container_data, content_str):
        print("❌ Could not find a target field (Content, Value, Body, Personalization, SysCharacter03) to inject into.")
        sys.exit(1)

    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(container_data, f, indent=2, ensure_ascii=False)

    print(f"✅ Built artifact in place: {config_path}")

    if isinstance(container_data, dict):
        zip_path = package_layer_zip(config_path, container_data)
        if deploy:
            deploy_layer(
                zip_path=zip_path,
                layer_data=container_data,
                env=env,
                user=user,
                company=company,
                tenant_company=tenant_company,
                plant=plant,
                timeout=timeout,
                skip_delete=skip_delete or import_only,
                skip_import=delete_only,
            )

def main():
    parser = argparse.ArgumentParser(description="Extract/Inject stringified JSON content.")
    parser.add_argument("action", choices=['extract', 'inject', 'build'], help="Action to perform")
    parser.add_argument("file", help="The container file (e.g. Layout.jsonc)")
    parser.add_argument("--deploy", action='store_true', help="For build action: delete then import the built layer zip.")
    parser.add_argument("--env", help="Environment nickname for deploy.")
    parser.add_argument("--user", help="User ID for deploy session.")
    parser.add_argument("--company", help="Company override for deploy.")
    parser.add_argument("--tenant-company", help="Target tenant company ID for global record operations.")
    parser.add_argument("--plant", default='', help="Plant ID for deploy callSettings header.")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout in seconds for deploy calls.")
    parser.add_argument("--skip-delete", action='store_true', help="Deploy without pre-delete step.")
    parser.add_argument("--delete-only", action='store_true', help="For build --deploy: run delete operation only and skip import.")
    parser.add_argument("--import-only", action='store_true', help="For build --deploy: run import operation only and skip delete.")
    
    args = parser.parse_args()
    env_name = args.env or os.environ.get('KIN_ENV_NAME')
    user_id = args.user or os.environ.get('KIN_USER') or os.environ.get('KIN_USER_ID')
    company_id = args.company or os.environ.get('KIN_COMPANY')
    tenant_company_id = args.tenant_company or os.environ.get('KIN_TENANT_COMPANY')

    file_path = args.file
    base, ext = os.path.splitext(file_path)
    content_path = f"{base}.content.jsonc"

    if args.action == 'build':
        if args.delete_only and args.import_only:
            print("❌ --delete-only and --import-only cannot be used together.")
            sys.exit(1)
        build_layer(
            config_path=file_path,
            deploy=args.deploy,
            env=env_name,
            user=user_id,
            company=company_id,
            tenant_company=tenant_company_id,
            plant=args.plant,
            timeout=args.timeout,
            skip_delete=args.skip_delete,
            delete_only=args.delete_only,
            import_only=args.import_only,
        )
        return

    if not os.path.exists(file_path):
        print(f"Error: File {file_path} not found.")
        sys.exit(1)

    container_data = load_jsonc(file_path)

    if args.action == 'extract':
        key, content_obj = find_and_extract(container_data)
        if content_obj:
            with open(content_path, 'w', encoding='utf-8') as f:
                json.dump(content_obj, f, indent=2, ensure_ascii=False)
            print(f"✅ Extracted '{key}' to {content_path}")
        else:
            print("❌ No valid stringified JSON field found to extract.")
            sys.exit(1)

    elif args.action == 'inject':
        if not os.path.exists(content_path):
            print(f"Error: Content file {content_path} not found.")
            sys.exit(1)
        
        with open(content_path, 'r', encoding='utf-8') as f:
            content_raw = strip_comments(f.read())
            # Parse and dump again to ensure consistent minification/formatting if desired, 
            # but usually we want to keep it somewhat clean.
            # For injection, we MUST dump it to a string.
            try:
                content_obj = json.loads(content_raw)
                content_str = stringify_json(content_obj)
            except json.JSONDecodeError as e:
                print(f"Error parsing content file: {e}")
                sys.exit(1)

        if inject_content(container_data, content_str):
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(container_data, f, indent=2, ensure_ascii=False)
            print(f"✅ Injected content into {file_path}")
        else:
            print("❌ Could not find a target field (Content, Value, etc.) to inject into.")
            sys.exit(1)

if __name__ == "__main__":
    main()