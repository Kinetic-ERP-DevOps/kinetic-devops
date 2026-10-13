# File: kinetic_devops/boreader_service.py
"""
BOReader Service Wrapper

Kinetic BOReader (Ice.Lib.BOReaderSvc) client with CLI support.
Handles GetList operations for querying Business Objects.

Design: Defensive API using dict for flexible request composition.
Inherits session management from KineticBaseClient.
"""

import sys
import json
import argparse
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
from typing import Dict, Any, Optional

if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient


class KineticBOReaderService(KineticBaseClient):
    """
    BOReader service inheriting session management from BaseClient.
    
    Provides convenient methods for querying Business Objects via BOReader.
    """
    
    def get_list(self, service_namespace: str, request_dict: Dict[str, Any] = None,
                 company: str = "", timeout: int = 30) -> Dict[str, Any]:
        """
        Fetch records via BOReader.GetList endpoint.
        
        Args:
            service_namespace: BO namespace (e.g., 'Ice.Lib.SomeSvc')
            request_dict: Request body as dict with optional keys:
                - 'whereClause': WHERE filter (optional, default '')
                - 'columnList': List of columns to return (optional, default [])
                - Any other fields the service expects
            company: Company override (uses self.config['company'] if empty)
            timeout: Request timeout in seconds
            
        Returns:
            Response dict from API (typically contains 'value' key with records)
            
        Raises:
            requests.HTTPError: On non-2xx status
        """
        if request_dict is None:
            request_dict = {}
        
        target_co = company if company else self.config['company']
        endpoint = f"{self.config['url']}/api/v2/odata/{target_co}/Ice.Lib.BOReaderSvc/GetList"
        
        # Build request body with required fields
        payload = {
            'serviceNamespace': service_namespace,
            'whereClause': request_dict.get('whereClause', ''),
            'columnList': request_dict.get('columnList', [])
        }
        payload.update({k: v for k, v in request_dict.items() 
                       if k not in ['whereClause', 'columnList']})
        
        # Build BOReader-specific headers
        headers = self.build_headers(self.config['token'], self.config['api_key'], target_co)
        headers['SessionInfo'] = json.dumps({'SessionID': self.config['token']})
        headers['callSettings'] = json.dumps({
            'pageSize': 0,
            'absolutePage': 0,
            'includeMetadata': True
        })
        
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        
        if not resp.ok:
            self.log_wire('POST', endpoint, headers, payload, resp)
            resp.raise_for_status()
        
        return resp.json() if resp.text else {}


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Query Kinetic BOReader Service: calls Ice.Lib.BOReaderSvc/GetList for a business-object\n"
            "service (read-only query) and saves the returned 'value' rows as a JSON array to a local file.\n"
            "The output file always uses the 'timestamp' conflict strategy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.boreader Erp.BO.PartSvc --env MyTestEnv --where \"PartNum = 'ABC'\"\n"
            "  python -m kinetic_devops.boreader Erp.BO.PartSvc --env MyTestEnv --columns PartNum,PartDescription"
        ),
    )
    parser.add_argument("service", metavar="SERVICE", help="Service namespace (e.g., Ice.Lib.SomeSvc or Erp.BO.PartSvc)")
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
    parser.add_argument("--where", metavar="CLAUSE", help="WHERE clause filter (default: none, all rows)", default="")
    parser.add_argument("--columns", metavar="COL1,COL2", help="Comma-separated columns to return (default: service default)")
    parser.add_argument("--co", metavar="COMPANY", help="Company Override (default: the session's company)")
    parser.add_argument("--out", metavar="FILE", help="Output filename (default: <service_with_underscores>_result.json)")
    parser.add_argument("--debug", action="store_true", help="Enable debug output and print tracebacks on error.")
    KineticBaseClient.add_file_resolution_args(parser)
    args = parser.parse_args()

    try:
        # 1. Initialize the service
        service = KineticBOReaderService(args.env, args.user, debug=args.debug)
        service.configure_file_resolution_from_args(args)
        
        # 2. Build request dict
        request_dict = {'whereClause': args.where}
        if args.columns:
            request_dict['columnList'] = [c.strip() for c in args.columns.split(',')]
        
        # 3. Execute query
        result = service.get_list(
            service_namespace=args.service,
            request_dict=request_dict,
            company=args.co
        )
        
        # 4. Save to file
        records = result.get('value', [])
        out_file = args.out or f"{args.service.replace('.', '_')}_result.json"
        out_file = service.resolve_output_path(out_file, conflict_resolution="timestamp")
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(records, f, indent=4)
            
        print(f"✅ Success: {len(records)} records saved to {out_file}")

    except Exception as e:
        print(f"❌ Script Error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
