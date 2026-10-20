import argparse
import json
import base64
import sys
import os
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient

class KineticEFxService(KineticBaseClient):
    """
    Service for executing Epicor Functions (EFx) directly.
    Useful for bypassing UI/App constraints or automating function calls.
    """
    
    def run_function(self, library: str, function: str, input_data: dict = None, company: str = "") -> dict:
        """
        Executes an Epicor Function via REST API.
        """
        target_co = company if company else self.config['company']
        # Construct the EFx endpoint: /api/v2/efx/{company}/{library}/{function}
        url = f"{self.config['url'].rstrip('/')}/api/v2/efx/{target_co}/{library}/{function}"
        
        headers = self.mgr.get_auth_headers({**self.config, "company": target_co})

        # EFx calls are POST requests
        response = requests.post(url, json=input_data or {}, headers=headers, timeout=120)
        
        if not response.ok:
            self.log_wire("POST", url, headers, body=input_data, resp=response)
            response.raise_for_status()
            
        return response.json()

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Execute Epicor Functions (EFx) and handle file outputs.\n"
            "POSTs --input to /api/v2/efx/<company>/<LIBRARY>/<FUNCTION> and prints the JSON response. The\n"
            "function runs server-side with whatever side effects it has. With --out, a ZipBase64/File/Content\n"
            "response field is written to that file instead (always 'timestamp' conflict strategy)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.efx MyLibrary MyFunction --env MyTestEnv --input '{\"PartNum\": \"ABC\"}'\n"
            "  python -m kinetic_devops.efx ExportAllTheThings ExportAllKineticCustomizationLayers "
            "--env MyTestEnv --out layers.zip"
        ),
    )
    parser.add_argument("library", metavar="LIBRARY", help="Library ID (e.g., ExportAllTheThings)")
    parser.add_argument("function", metavar="FUNCTION", help="Function ID (e.g., ExportAllKineticCustomizationLayers)")
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
    parser.add_argument("--co", metavar="COMPANY", help="Company Override (default: the session's company)")
    parser.add_argument("--input", metavar="JSON", help="Input JSON object string passed as the request body (default: {})", default="{}")
    parser.add_argument(
        "--out", metavar="FILE",
        help="Output filename (for file/zip responses). Without it, or if the response has no "
             "ZipBase64/File/Content field, the response JSON is printed.",
    )
    parser.add_argument("--decode", action="store_true", help="Decode the output as Base64 (auto-detected for ZipBase64)")
    parser.add_argument("--debug", action="store_true", help="Enable debug output.")
    KineticBaseClient.add_file_resolution_args(parser)
    
    args = parser.parse_args()

    try:
        service = KineticEFxService(args.env, args.user, debug=args.debug)
        service.configure_file_resolution_from_args(args)
        
        try:
            input_data = json.loads(args.input)
        except json.JSONDecodeError:
            print("❌ Error: Invalid JSON provided for --input")
            sys.exit(1)
            
        print(f"🚀 Executing {args.library}.{args.function}...")
        result = service.run_function(args.library, args.function, input_data, args.co)
        
        # Check for common file output keys
        output_content = result.get("ZipBase64") or result.get("File") or result.get("Content")
        
        if args.out and output_content:
            # Auto-decode if it looks like Base64 or if requested
            if args.decode or "ZipBase64" in result:
                file_data = base64.b64decode(output_content)
                mode = "wb"
            else:
                file_data = output_content
                mode = "w"
                
            args.out = service.resolve_output_path(args.out, conflict_resolution="timestamp")
            with open(args.out, mode) as f:
                f.write(file_data)
            print(f"✅ Output saved to {args.out}")
        else:
            print(json.dumps(result, indent=2))

    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()