"""
kinetic_devops/file_service.py

FileService Wrapper - Manage Epicor Kinetic file and storage operations.

Inherits session management from KineticBaseClient.
Provides high-level methods to:
- Update DMS storage type configurations
- Manage file storage settings
- Query file service metadata
- Validate file storage availability
"""

import sys
import json
import argparse
from typing import Dict, Any, Optional, List

from kinetic_devops import _lazy_import

requests = _lazy_import("requests")

if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient


class KineticFileService(KineticBaseClient):
    """
    File service inheriting session management from BaseClient.
    
    Manages Epicor Kinetic DMS (Document Management System) operations.
    Responsibilities:
    - Update DMS storage type configurations
    - Manage file storage metadata
    - Query file storage availability and status
    - Validate storage configurations
    """
    
    def get_dms_storage_types(self, company: str = "") -> Optional[List[Dict[str, Any]]]:
        """
        Fetch all DMS storage type configurations for a company.
        
        Args:
            company: Company ID override (uses self.config['company'] if empty)
        
        Returns:
            List of DMS storage type records, or None on failure
        """
        target_co = company if company else self.config['company']
        
        try:
            url = f"{self.config['url']}/api/v2/odata/{target_co}/Erp.BO.DMSTypeSvc/DMSTypes"
            headers = self.build_headers(self.config['token'], self.config['api_key'], target_co)
            
            resp = requests.get(url, headers=headers, timeout=20)
            
            if not resp.ok:
                self.log_wire("GET", url, headers, resp=resp)
                resp.raise_for_status()
            
            data = resp.json()
            records = data.get('value', [])
            
            return records
        
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch DMS storage types for {target_co}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error fetching DMS types: {e}")
            return None
    
    def update_dms_storage_type(
        self,
        dms_type_id: str,
        storage_config: Dict[str, Any],
        company: str = "",
        continue_on_error: bool = True,
        rollback_on_child_error: bool = True
    ) -> bool:
        """
        Update a single DMS storage type configuration.
        
        Args:
            dms_type_id: DMS Type ID to update
            storage_config: Updated configuration dictionary
            company: Company ID override (uses self.config['company'] if empty)
            continue_on_error: Continue if errors occur
            rollback_on_child_error: Rollback parent on child errors
        
        Returns:
            True if successful, False otherwise
        """
        target_co = company if company else self.config['company']
        
        try:
            url = f"{self.config['url']}/api/v2/odata/{target_co}/Erp.BO.DMSTypeSvc/UpdateExt"
            headers = self.build_headers(self.config['token'], self.config['api_key'], target_co)
            headers['Content-Type'] = 'application/json'
            
            # Ensure RowMod is set to "U" for update
            update_record = {**storage_config, "RowMod": "U"}
            
            payload = {
                "ds": {
                    "DMSType": [update_record]
                },
                "continueProcessingOnError": continue_on_error,
                "rollbackParentOnChildError": rollback_on_child_error
            }
            
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            
            if not resp.ok:
                self.log_wire("POST", url, headers, payload, resp)
                resp.raise_for_status()
            
            data = resp.json()
            
            if data.get('errorsOccurred'):
                print(f"UpdateExt returned errors for {dms_type_id}: {data}")
                return False
            
            return True
        
        except requests.exceptions.RequestException as e:
            print(f"UpdateExt request failed for {dms_type_id}: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error updating DMS type: {e}")
            return False
    
    def update_dms_storage_types(
        self,
        records: List[Dict[str, Any]],
        company: str = "",
        continue_on_error: bool = True,
        rollback_on_child_error: bool = True
    ) -> bool:
        """
        Batch update DMS storage type configurations.
        
        Args:
            records: List of records to update (will set RowMod="U" for each)
            company: Company ID override (uses self.config['company'] if empty)
            continue_on_error: Continue if errors occur
            rollback_on_child_error: Rollback parent on child errors
        
        Returns:
            True if successful, False otherwise
        """
        target_co = company if company else self.config['company']
        
        if not records:
            return True
        
        try:
            # Mark all records for update
            update_records = [
                {**record, "RowMod": "U"}
                for record in records
            ]
            
            url = f"{self.config['url']}/api/v2/odata/{target_co}/Erp.BO.DMSTypeSvc/UpdateExt"
            headers = self.build_headers(self.config['token'], self.config['api_key'], target_co)
            headers['Content-Type'] = 'application/json'
            
            payload = {
                "ds": {
                    "DMSType": update_records
                },
                "continueProcessingOnError": continue_on_error,
                "rollbackParentOnChildError": rollback_on_child_error
            }
            
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            
            if not resp.ok:
                self.log_wire("POST", url, headers, payload, resp)
                resp.raise_for_status()
            
            data = resp.json()
            
            if data.get('errorsOccurred'):
                print(f"UpdateExt returned errors for {target_co}: {data}")
                return False
            
            return True
        
        except requests.exceptions.RequestException as e:
            print(f"UpdateExt request failed for {target_co}: {e}")
            return False
        except Exception as e:
            print(f"Unexpected error in update_dms_storage_types: {e}")
            return False
    
    def set_default_storage_type(self, dms_type_id: str, company: str = "") -> bool:
        """
        Mark a DMS storage type as the default for the company.
        
        Args:
            dms_type_id: DMS Type ID to set as default
            company: Company ID override (uses self.config['company'] if empty)
        
        Returns:
            True if successful, False otherwise
        """
        target_co = company if company else self.config['company']
        
        # Fetch current DMS types
        records = self.get_dms_storage_types(target_co)
        
        if records is None:
            return False
        
        # Find the record to set as default
        target = None
        others = []
        
        for record in records:
            if record.get('DMSTypeID') == dms_type_id:
                target = {**record, "IsDefault": True, "RowMod": "U"}
            else:
                # Unset other defaults
                if record.get('IsDefault'):
                    others.append({**record, "IsDefault": False, "RowMod": "U"})
        
        if target is None:
            print(f"DMS Type {dms_type_id} not found for {target_co}")
            return False
        
        # Batch update: target as default, others cleared
        update_list = [target] + others
        return self.update_dms_storage_types(update_list, target_co)
    
    def get_file_service_status(self, company: str = "") -> Optional[Dict[str, Any]]:
        """
        Get file service status and configuration summary for a company.
        
        Args:
            company: Company ID override (uses self.config['company'] if empty)
        
        Returns:
            Dictionary with file service status, or None on failure
        """
        target_co = company if company else self.config['company']
        
        try:
            types = self.get_dms_storage_types(target_co)
            
            if types is None:
                return None
            
            # Build status summary
            status = {
                "total_storage_types": len(types),
                "storage_types": [],
                "default_type": None,
                "is_available": len(types) > 0
            }
            
            for t in types:
                type_info = {
                    "DMSTypeID": t.get('DMSTypeID'),
                    "IsDefault": t.get('IsDefault', False),
                    "StorageType": t.get('StorageType')  # e.g., "LocalFileSystem", "AzureBlob"
                }
                status["storage_types"].append(type_info)
                
                if t.get('IsDefault'):
                    status["default_type"] = t.get('DMSTypeID')
            
            return status
        
        except Exception as e:
            print(f"Error getting file service status: {e}")
            return None


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Manage Kinetic File Service (DMS) Configuration via Erp.BO.DMSTypeSvc.\n"
            "  list         read all DMS storage types and save them to a JSON file (default action)\n"
            "  status       save a summary (count, default type, storage types) to a JSON file\n"
            "  set-default  WRITES TO EPICOR: mark --dms-type-id as the company's default storage type\n"
            "               and clear IsDefault on any other type (UpdateExt)\n"
            "Output files always use the 'timestamp' conflict strategy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.file_service --env MyTestEnv --action status\n"
            "  python -m kinetic_devops.file_service --env MyTestEnv --action set-default --dms-type-id MyDmsType"
        ),
    )
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
    parser.add_argument("--action", choices=['list', 'status', 'set-default'],
                       help="Action to perform: list, status or set-default (see above; default: list)", default='list')
    parser.add_argument("--dms-type-id", metavar="ID", help="DMS Type ID (required for set-default)")
    parser.add_argument(
        "--out", metavar="FILE",
        help="Output filename for list/status (default: dms_storage_types.json / file_service_status.json)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug output.")
    KineticBaseClient.add_file_resolution_args(parser)
    args = parser.parse_args()

    try:
        # 1. Initialize the service
        service = KineticFileService(args.env, args.user, debug=args.debug)
        service.configure_file_resolution_from_args(args)
        
        # 2. Execute action
        if args.action == 'list':
            types = service.get_dms_storage_types(args.co)
            if types is None:
                print("❌ Failed to fetch DMS storage types")
                sys.exit(1)
            
            out_file = args.out or "dms_storage_types.json"
            out_file = service.resolve_output_path(out_file, conflict_resolution="timestamp")
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(types, f, indent=4)
            print(f"✅ Success: {len(types)} DMS storage type(s) saved to {out_file}")
        
        elif args.action == 'status':
            status = service.get_file_service_status(args.co)
            if status is None:
                print("❌ Failed to fetch file service status")
                sys.exit(1)
            
            out_file = args.out or "file_service_status.json"
            out_file = service.resolve_output_path(out_file, conflict_resolution="timestamp")
            with open(out_file, 'w', encoding='utf-8') as f:
                json.dump(status, f, indent=4)
            
            print(f"✅ Success: File service status saved to {out_file}")
            print(f"   Total types: {status['total_storage_types']}")
            print(f"   Default type: {status['default_type']}")
            print(f"   Available: {status['is_available']}")
        
        elif args.action == 'set-default':
            if not args.dms_type_id:
                print("❌ --dms-type-id required for set-default action")
                sys.exit(1)
            
            success = service.set_default_storage_type(args.dms_type_id, args.co)
            if success:
                print(f"✅ Success: Set {args.dms_type_id} as default")
            else:
                print(f"❌ Failed to set {args.dms_type_id} as default")
                sys.exit(1)

    except Exception as e:
        print(f"❌ Script Error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
