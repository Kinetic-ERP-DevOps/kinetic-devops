#!/usr/bin/env python3
"""
scripts/tax_clear.py

Clear tax service configurations from Epicor Kinetic for one or more companies.

Supports:
- Interactive mode (prompts for environment and company)
- Non-interactive mode (CLI arguments)
- Multiple companies in a single run
- Logging to tax_clear.log

Usage:
    Interactive:
        python tax_clear.py
    
    Non-interactive:
        python tax_clear.py --env DEV --company COMPANY_ID
        python tax_clear.py --env DEV --company COMPANY_ID COMPANY_ID --inactive-only
    
    Get help:
        python tax_clear.py --help
"""

import sys
import logging
import argparse
from pathlib import Path

from kinetic_devops.auth import KineticConfigManager
from kinetic_devops.tax_service import TaxService



def setup_logging():
    """Configure logging for tax clearing operations."""
    log_file = Path(__file__).parent / "tax_clear.log"
    
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


def clear_company_tax_configs(env_nickname: str, company: str, inactive_only: bool = False) -> bool:
    """
    Clear tax service configurations for a single company.
    
    Args:
        env_nickname: Environment nickname (e.g., "ENV")
        company: Company ID (e.g., "COMP")
        inactive_only: Only clear inactive (TaxConnectEnabled=false) configs
    
    Returns:
        True if successful, False otherwise
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
        
        log_info(f"Clearing tax configs in-place for {env_nickname}/{company} (inactive_only={inactive_only})")
        
        # Use TaxService to handle operations
        tax_service = TaxService(url, token, api_key, debug=False)
        
        if inactive_only:
            log_debug(f"Fetching inactive tax configs for {company}")
            records = tax_service.get_inactive_configs(company)
            if records is not None:
                log_info(f"Found {len(records)} inactive tax config(s) for {company}")
                success = tax_service.update_configs(company, records)
            else:
                log_error(f"Failed to fetch inactive configs for {company}")
                success = False
        else:
            log_debug(f"Fetching all tax configs for {company}")
            success = tax_service.clear_all_configs(company)
        
        return success
    
    except Exception as e:
        log_error(f"Exception in clear_company_tax_configs: {e}")
        return False


def clear_multiple_companies(env_nickname: str, companies: list, inactive_only: bool = False) -> dict:
    """
    Clear tax configs for multiple companies.
    
    Args:
        env_nickname: Environment nickname
        companies: List of company IDs
        inactive_only: Only clear inactive configs
    
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
        success = clear_company_tax_configs(env_nickname, company, inactive_only)
        results["details"][company] = "success" if success else "failed"
        
        if success:
            results["successful"] += 1
        else:
            results["failed"] += 1
    
    return results


def main():
    """Main entry point for tax clearing."""
    parser = argparse.ArgumentParser(
        description="Clear tax service configurations from Epicor Kinetic"
    )
    
    parser.add_argument(
        '--env',
        help='Environment nickname (e.g., Dev, Production)'
    )
    
    parser.add_argument(
        '--company',
        nargs='+',
        help='Company ID(s) to process (e.g., ACME-LAB ACHME-DIST ACME)'
    )
    
    parser.add_argument(
        '--inactive-only',
        action='store_true',
        help='Only clear inactive (TaxConnectEnabled=false) configurations'
    )
    
    args = parser.parse_args()
    
    log_info("=" * 70)
    log_info("Tax Config Clearing Tool")
    log_info("=" * 70)
    
    try:
        if args.env and args.company:
            # Non-interactive mode
            env_nickname = args.env
            companies = args.company
            results = clear_multiple_companies(env_nickname, companies, args.inactive_only)
        
        else:
            # Interactive mode
            mgr = KineticConfigManager(debug=False)
            env_nickname, _, _ = mgr.prompt_for_env()

            if not env_nickname:
                log_error("Failed to get environment configuration")
                return 1
            
            # Prompt for companies
            companies_input = input("\nEnter company ID(s) to clear (space-separated, e.g., 'ACME-LABS ACME'): ").strip()
            companies = companies_input.split()
            
            if not companies:
                log_error("No companies specified")
                return 1
            
            results = clear_multiple_companies(env_nickname, companies, args.inactive_only)
        
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