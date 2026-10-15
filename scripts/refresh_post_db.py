#!/usr/bin/env python3
"""
scripts/refresh_post_db.py

Post-database refresh updates for Epicor Kinetic.

Performs:
- DMS (Document Management System) storage type updates
- Tax configuration clearing
- Multi-company batch operations
- Session-aware token management via SDK

Usage:
    Interactive:
        python refresh_post_db.py
    
    Non-interactive:
        python refresh_post_db.py --env DEV --company ACME-LABS
        python refresh_post_db.py --env DEV --company ACME-LABS ICE
    
    Get help:
        python refresh_post_db.py --help
"""

import sys
import logging
import argparse
from pathlib import Path

from kinetic_devops.auth import KineticConfigManager
from kinetic_devops.tax_service import TaxService
from kinetic_devops.file_service import FileService


def setup_logging():
    """Configure logging for refresh operations."""
    log_file = Path(__file__).parent / "refresh_post_db.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )
    
    return logging.getLogger(__name__)


logger = setup_logging()


def log_info(msg: str):
    """Log an info message."""
    logger.info(msg)


def log_debug(msg: str):
    """Log a debug message."""
    logger.debug(msg)


def log_error(msg: str):
    """Log an error message."""
    logger.error(msg)


def update_company(env_nickname: str, company: str) -> bool:
    """
    Perform all refresh operations for a single company.
    
    Args:
        env_nickname: Environment nickname (e.g., "Dev")
        company: Company ID (e.g., "ACME-LABS")
    
    Returns:
        True if all operations succeeded, False otherwise
    """
    try:
        # Get active configuration (session-aware, will prompt if needed)
        mgr = KineticConfigManager(debug=False)
        url, token, api_key = mgr.get_active_config(
            {"nickname": env_nickname},
            fields=("url", "token", "api_key"),
        )

        if not all([url, token, api_key]):
            log_error(f"Missing required config fields for {env_nickname}")
            return False
        
        log_info(f"Updating {env_nickname}/{company}")
        
        # Initialize services
        tax_service = TaxService(url, token, api_key, debug=False)
        file_service = FileService(url, token, api_key, debug=False)
        
        # Operation 1: Update DMS storage types
        log_debug(f"Fetching DMS storage types for {company}")
        dms_records = file_service.get_dms_storage_types(company)
        
        if dms_records is None:
            log_error(f"Failed to fetch DMS storage types for {company}")
            dms_ok = False
        elif len(dms_records) == 0:
            log_info(f"No DMS storage types to update for {company}")
            dms_ok = True
        else:
            # Refresh DMS records (they may need metadata updates)
            log_debug(f"Updating {len(dms_records)} DMS storage type(s) for {company}")
            dms_ok = file_service.update_dms_storage_types(company, dms_records)
        
        # Operation 2: Clear tax configurations in-place (non-destructive)
        log_debug(f"Fetching tax configurations for {company}")
        tax_records = tax_service.get_tax_configs(company)
        
        if tax_records is None:
            log_error(f"Failed to fetch tax configs for {company}")
            tax_ok = False
        elif len(tax_records) == 0:
            log_info(f"No tax configs to clear for {company}")
            tax_ok = True
        else:
            log_debug(f"Updating {len(tax_records)} tax config(s) in-place for {company}")
            tax_ok = tax_service.update_configs(company, tax_records)
        
        if dms_ok and tax_ok:
            log_info(f"✅ Successfully updated {company}")
            return True
        else:
            log_error(f"❌ Failed to update {company} (DMS: {dms_ok}, Tax: {tax_ok})")
            return False
    
    except Exception as e:
        log_error(f"Exception updating {company}: {e}")
        return False


def refresh_multiple_companies(env_nickname: str, companies: list) -> dict:
    """
    Perform refresh updates for multiple companies.
    
    Args:
        env_nickname: Environment nickname
        companies: List of company IDs
    
    Returns:
        Dictionary with success/failure counts
    """
    results = {
        "total": len(companies),
        "successful": 0,
        "failed": 0,
        "details": {}
    }
    
    log_info(f"Processing {len(companies)} company(ies): {', '.join(companies)}")
    
    for company in companies:
        success = update_company(env_nickname, company)
        results["details"][company] = "success" if success else "failed"
        
        if success:
            results["successful"] += 1
        else:
            results["failed"] += 1
    
    return results


def main():
    """Main entry point for refresh updates."""
    parser = argparse.ArgumentParser(
        description="Post-database refresh updates for Epicor Kinetic"
    )
    
    parser.add_argument(
        '--env',
        help='Environment nickname (e.g., Dev, Production)'
    )
    
    parser.add_argument(
        '--company',
        nargs='+',
        help='Company ID(s) to process (e.g., ACME-LABS ICE ACME)'
    )
    
    args = parser.parse_args()
    
    log_info("=" * 70)
    log_info("Post-Database Refresh Updates")
    log_info("=" * 70)
    
    try:
        if args.env and args.company:
            # Non-interactive mode
            env_nickname = args.env
            companies = args.company
            results = refresh_multiple_companies(env_nickname, companies)
        
        else:
            # Interactive mode
            mgr = KineticConfigManager(debug=False)
            env_nickname, _, _ = mgr.prompt_for_env()

            if not env_nickname:
                log_error("Failed to get environment configuration")
                return 1
            
            # Prompt for companies
            companies_input = input("\nEnter company ID(s) to update (space-separated, e.g., 'ACME-LABS ICE'): ").strip()
            companies = companies_input.split()
            
            if not companies:
                log_error("No companies specified")
                return 1
            
            results = refresh_multiple_companies(env_nickname, companies)
        
        # Print summary
        log_info("=" * 70)
        log_info(f"Results: {results['successful']}/{results['total']} successful")
        
        for company, status in results["details"].items():
            log_info(f"  {company}: {status}")
        
        log_info("=" * 70)
        
        return 0 if results["failed"] == 0 else 1
    
    except KeyboardInterrupt:
        log_info("\nAborted by user")
        return 1
    except Exception as e:
        log_error(f"Unexpected error in main: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())