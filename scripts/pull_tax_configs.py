#!/usr/bin/env python3
"""Pull tax config records for all stored Kinetic environments and companies.

Writes JSON files to `Data/tax_configs/<nickname>__<company>.json`.

Usage:
  python scripts/pull_tax_configs.py [--env ENV] [--out-dir PATH]

The script will attempt to resolve a valid token for each environment using
the stored sessions in the keyring. If no valid token exists, you'll be
prompted to authenticate (interactive password prompt) by the config manager.
"""
from pathlib import Path
import json
import argparse
import sys

from kinetic_devops.auth import KineticConfigManager
from kinetic_devops.tax_service import TaxService


def ensure_outdir(p: Path):
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Pull tax configs for all companies")
    parser.add_argument("--env", help="Only pull for this environment nickname", default=None)
    parser.add_argument("--out-dir", help="Output directory", default="Data/tax_configs")
    parser.add_argument("--dry-run", help="Do not write files; just print summary", action="store_true")
    args = parser.parse_args()

    outdir = Path(args.out_dir)
    ensure_outdir(outdir)

    mgr = KineticConfigManager(debug=False)

    servers = mgr._get_server_dict()
    if not servers:
        print("No stored Kinetic environments found. Use scripts/env_init.bat or auth.store() to add one.")
        sys.exit(1)

    targets = [args.env] if args.env else list(servers.keys())

    summary = []

    for nick in targets:
        if nick not in servers:
            print(f"- Skipping unknown environment '{nick}'")
            continue

        base = mgr.get_base_config(nick)
        if not base:
            print(f"- Could not read base config for {nick}; skipping.")
            continue

        url = base.get('url')
        companies_raw = base.get('companies') or ''
        api_key = base.get('api_key') or ''
        companies = [c.strip() for c in str(companies_raw).split(',') if c.strip()]

        # Attempt to resolve a token for this environment (may prompt interactively)
        url_t, token, api_key_t, active_company = mgr.get_active_config(nick, fields=("url","token","api_key","company"))
        token = token or None
        if not token:
            print(f"- No token available for '{nick}'. You may be prompted to authenticate.")

        # Prefer explicit api_key from active config when present
        if api_key_t:
            api_key = api_key_t

        tax_svc = TaxService(base_url=url, token=token or "", api_key=api_key)

        for co in companies:
            print(f"Fetching tax configs for {nick} @ {co}...")
            records = tax_svc.get_tax_configs(co)
            filename = outdir / f"{nick}__{co}.json"
            ok = bool(records)

            if args.dry_run:
                summary.append({"env": nick, "company": co, "count": len(records) if records else 0, "status": "dry"})
                continue

            try:
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(records or [], f, indent=2, ensure_ascii=False)
                print(f"  -> Wrote {filename} ({len(records) if records else 0} records)")
                summary.append({"env": nick, "company": co, "count": len(records) if records else 0, "status": "ok"})
            except Exception as e:
                print(f"  ! Failed to write {filename}: {e}")
                summary.append({"env": nick, "company": co, "count": 0, "status": f"write-fail: {e}"})

    # Summarize
    print("\nSummary:")
    for s in summary:
        print(f" - {s['env']} / {s['company']}: {s['count']} records ({s['status']})")


if __name__ == '__main__':
    main()
