"""
kinetic_devops/baq.py

BAQ Service - Query Kinetic Business Analysis Queries.

Inherits session management from KineticBaseClient.
Provides methods to:
- Execute BAQs with filter and column selection
- Override company and plant context
- Export results to JSON
"""

# kinetic_devops/baq.py
import argparse
import json
import sys
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient

class KineticBAQService(KineticBaseClient):
    """BAQ service inheriting session management from BaseClient."""
    
    def get_baq_results(self, baq_name: str, query_params: str = "", company: str = "", plant: str = "", debug: bool = False) -> list[dict]:
        """Module API: Fetches BAQ data with support for context overrides."""
        target_co = company if company else self.config['company']
        
        # Prepare header config with the target company
        header_cfg = self.config.copy()
        header_cfg['company'] = target_co
        
        headers = self.mgr.get_auth_headers(header_cfg, plant_id=plant)
        url = f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/BaqSvc/{baq_name}/Data"
    
        if query_params: 
            url += f"?{query_params.lstrip('?')}"

        response = requests.get(url, headers=headers, timeout=60)
        
        # Trigger wire log if debug is on OR if the request failed
        if debug or not response.ok: 
            self.log_wire('GET', url, headers, resp=response)
        
        response.raise_for_status()
        return response.json().get('value', [])

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Kinetic BAQ Data: runs a BAQ through the OData BaqSvc/<BAQ>/Data endpoint (read-only GET)\n"
            "and saves the returned rows as a JSON array to a local file. The output file always uses the\n"
            "'timestamp' conflict strategy (an existing file is kept and a timestamped copy is written)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.baq MyBAQ --env MyTestEnv\n"
            "  python -m kinetic_devops.baq MyBAQ --env MyTestEnv --params '$top=10' --out rows.json"
        ),
    )
    parser.add_argument("baq", metavar="BAQ", help="BAQ Name (the BAQ ID as shown in BAQ Designer).")
    parser.add_argument(
        "--env", metavar="ENV",
        help="Environment Nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument(
        "--user", metavar="USER_ID",
        help="Specific User ID (Session) to use for ENV. Default: KINETIC_SESSION_USER / KIN_USER, or the "
             "only recorded session.",
    )
    parser.add_argument(
        "--params", metavar="QUERY",
        help="OData query string appended to the URL (e.g., '$filter=PartNum eq 'ABC'' or '$top=10'). "
             "Default: none.",
        default="",
    )
    parser.add_argument("--out", metavar="FILE", help="Output filename (default: <BAQ>.json in the current directory).")
    parser.add_argument("--co", metavar="COMPANY", help="Company Override (default: the session's company).")
    parser.add_argument("--plant", metavar="PLANT", help="Plant ID, sent as the X-Epicor-Plant header (default: none).")
    parser.add_argument("--debug", action="store_true", help="Log the HTTP exchange (redacted) and print tracebacks.")
    KineticBaseClient.add_file_resolution_args(parser)
    args = parser.parse_args()

    try:
        # 1. Initialize the service (Inherits env/user selection from Base)
        service = KineticBAQService(args.env, args.user)
        service.configure_file_resolution_from_args(args)
        
        # 2. Execute the BAQ
        data = service.get_baq_results(
            baq_name=args.baq, 
            query_params=args.params, 
            company=args.co, 
            plant=args.plant,
            debug=args.debug
        )
        
        # 3. Save to file
        out_file = args.out or f"{args.baq}.json"
        out_file = service.resolve_output_path(out_file, conflict_resolution="timestamp")
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
            
        print(f"✅ Success: {len(data)} records saved to {out_file}")

    except Exception as e:
        print(f"❌ Script Error: {e}")
        if args.debug:
            import traceback; traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()