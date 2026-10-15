"""
kinetic_devops/tax_service.py

TaxService - Manage Epicor Kinetic tax configurations.

Provides high-level methods to:
- Fetch tax service configurations
- Update tax configurations
- Clear tax records in-place (no delete)
- List inactive tax configurations
"""

import json
import warnings
from typing import List, Dict, Any, Optional
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")

from .KineticCore import KineticCore


class TaxService(KineticCore):
    """
    Service for managing Epicor Kinetic tax configurations.
    
    Responsibilities:
    - Fetch and parse tax service configurations
    - Build and execute UpdateExt calls for in-place updates
    - Handle company-scoped operations
    """
    
    def __init__(self, base_url: str, token: str, api_key: str, debug: bool = False):
        """
        Initialize TaxService.
        
        Args:
            base_url: Base URL of Kinetic instance (e.g., https://example.epicorsaas.com/SaaS000/)
            token: Bearer token for authentication
            api_key: Scoped API key
            debug: Enable debug logging
        """
        super().__init__(debug=debug)
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.api_key = api_key
    
    def _build_headers(self, company: str = "") -> Dict[str, str]:
        """Build standard headers for tax service API calls."""
        headers = self.build_headers(self.token, self.api_key, company)
        headers['accept'] = 'application/json'
        return headers
    
    def get_tax_configs(self, company: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch all tax service configurations for a company.
        
        Args:
            company: Company ID (e.g., "ACME-LABS")
        
        Returns:
            List of tax config records, or None on failure
        """
        try:
            url = f"{self.base_url}/api/v2/odata/{company}/Erp.BO.TaxSvcConfigSvc/TaxSvcConfigs"
            headers = self._build_headers(company)
            
            self.log_wire("GET", url, headers)
            
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            
            data = resp.json()
            self.log_wire("GET", url, headers, resp=resp)
            
            records = data.get('value', [])
            self.debug_log(f"Fetched {len(records)} tax config record(s) for {company}")
            
            return records
        
        except requests.exceptions.RequestException as e:
            self.debug_log(f"Failed to fetch tax configs for {company}: {e}")
            return None
        except Exception as e:
            self.debug_log(f"Unexpected error fetching tax configs: {e}")
            return None
    
    def get_inactive_configs(self, company: str) -> Optional[List[Dict[str, Any]]]:
        """
        Fetch only inactive (TaxConnectEnabled=false) tax configurations.
        
        Args:
            company: Company ID
        
        Returns:
            List of inactive tax config records, or None on failure
        """
        records = self.get_tax_configs(company)
        
        if records is None:
            return None
        
        # Filter for inactive records
        inactive = [
            r for r in records
            if not r.get('TaxConnectEnabled', False)
        ]
        
        self.debug_log(f"Found {len(inactive)} inactive tax config(s) for {company}")
        
        return inactive
    
    def update_configs(
        self, 
        company: str, 
        records: List[Dict[str, Any]],
        clear_values: Optional[Dict[str, Any]] = None,
        continue_on_error: bool = True,
        rollback_on_child_error: bool = True
    ) -> bool:
        """
        Update tax configuration records via UpdateExt.
        
        This method intentionally uses in-place updates (RowMod="U") so
        tax records are retained and cleared/disabled rather than deleted.
        
        Args:
            company: Company ID
            records: List of records to update (will set RowMod="U" for each)
            clear_values: Optional override values to apply while clearing
            continue_on_error: Continue processing if errors occur
            rollback_on_child_error: Rollback parent on child errors
        
        Returns:
            True if successful, False otherwise
        """
        if not records:
            self.debug_log(f"No records to update for {company}")
            return True
        
        try:
            # Preserve records and clear values in-place to avoid destructive deletes.
            values = {
                "TaxConnectEnabled": False,
                "URL": "",
                "Account": "",
                "Key": "",
            }
            if clear_values:
                values.update(clear_values)

            update_records = []
            for record in records:
                updated = {**record, "RowMod": "U"}
                for field, field_value in values.items():
                    updated[field] = field_value
                update_records.append(updated)
            
            url = f"{self.base_url}/api/v2/odata/{company}/Erp.BO.TaxSvcConfigSvc/UpdateExt"
            headers = self._build_headers(company)
            headers['Content-Type'] = 'application/json'
            
            payload = {
                "ds": {
                    "TaxSvcConfig": update_records
                },
                "continueProcessingOnError": continue_on_error,
                "rollbackParentOnChildError": rollback_on_child_error
            }
            
            self.log_wire("POST", url, headers, json.dumps(payload)[:200])
            
            resp = requests.post(url, headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            
            data = resp.json()
            self.log_wire("POST", url, headers, resp=resp)
            
            if data.get('errorsOccurred'):
                self.debug_log(f"UpdateExt returned errors for {company}: {data}")
                return False
            
            self.debug_log(f"Successfully updated {len(update_records)} record(s) for {company}")
            return True
        
        except requests.exceptions.RequestException as e:
            self.debug_log(f"UpdateExt request failed for {company}: {e}")
            return False
        except Exception as e:
            self.debug_log(f"Unexpected error in update_configs: {e}")
            return False

    def delete_configs(
        self,
        company: str,
        records: List[Dict[str, Any]],
        continue_on_error: bool = True,
        rollback_on_child_error: bool = True
    ) -> bool:
        """Backward-compatible alias that now performs in-place updates.

        Kept to avoid breaking external callers while retiring RowMod="D"
        behavior from this SDK.

        Warning:
            Record deletion may break downstream tax modules. Use clear/update
            behavior (RowMod="U" + empty values) when no config is available.
        """
        warning_msg = (
            "delete_configs was requested. Deleting tax records may break tax "
            "modules. Preferred behavior is clear-in-place updates with empty "
            "values when no configuration is available."
        )
        warnings.warn(warning_msg, category=UserWarning, stacklevel=2)
        self.debug_log(warning_msg)
        self.debug_log("delete_configs is deprecated; applying in-place clear update instead.")
        return self.update_configs(
            company=company,
            records=records,
            continue_on_error=continue_on_error,
            rollback_on_child_error=rollback_on_child_error,
        )
    
    def clear_all_configs(self, company: str) -> bool:
        """
        Fetch all tax configs and clear them in-place.
        
        Args:
            company: Company ID
        
        Returns:
            True if successful, False otherwise
        """
        records = self.get_tax_configs(company)
        
        if records is None:
            return False
        
        if not records:
            self.debug_log(f"No tax configs to clear for {company}")
            return True
        
        return self.update_configs(company, records)
    
    def clear_inactive_configs(self, company: str) -> bool:
        """
        Fetch inactive tax configs and clear them in-place.
        
        Args:
            company: Company ID
        
        Returns:
            True if successful, False otherwise
        """
        records = self.get_inactive_configs(company)
        
        if records is None:
            return False
        
        if not records:
            self.debug_log(f"No inactive tax configs to clear for {company}")
            return True
        
        return self.update_configs(company, records)
