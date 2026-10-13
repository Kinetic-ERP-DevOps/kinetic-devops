"""
Customer data extraction utility.

Demonstrates how to use kinetic-devops BAQ client to export customer
master data from Epicor Kinetic to a JSON file for analysis,
migration, or backup purposes.

This is a simple example of a reusable function that:
1. Uses KineticConfigManager for authentication
2. Executes a BAQ to retrieve data
3. Exports results to JSON
4. Returns operation summary

Usage Examples:
    # Export all customers (default: 10,000 limit)
    python loader.py --run functions.pull_customers.export_customers
    
    # With custom arguments
    python loader.py --run functions.pull_customers.export_customers \\
      --args '{"output_file": "my_customers.json", "limit": 5000}'
    
    # In Python code
    from functions.pull_customers import export_customers, get_customer_count
    
    results = export_customers(output_file='customers.json')
    count = get_customer_count()
"""

__metadata__ = {
    'name': 'Customer Exporter',
    'version': '1.0.0',
    'author': 'Kinetic Team',
    'tags': ['export', 'customers', 'baq', 'data-extraction'],
    'dependencies': ['kinetic_devops.baq', 'kinetic_devops.auth'],
    'usage_example': '''
from functions.pull_customers import export_customers
results = export_customers(limit=1000)
print(results)
    ''',
}

import json
from typing import Dict, List, Optional, Any


# Example metadata - in real usage would connect to Epicor
MOCK_CUSTOMERS = [
    {
        'CustomerID': 'CUST001',
        'CustomerName': 'ABC Manufacturing',
        'Address': '123 Main St',
        'City': 'Portland',
        'State': 'OR',
        'Country': 'USA',
    },
    {
        'CustomerID': 'CUST002',
        'CustomerName': 'XYZ Distribution',
        'Address': '456 Oak Ave',
        'City': 'Seattle',
        'State': 'WA',
        'Country': 'USA',
    },
]


def export_customers(
    output_file: str = 'customers.json',
    limit: Optional[int] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Export customer master data to JSON file.
    
    Args:
        output_file: Output JSON file path
        limit: Maximum number of customers to export (None = all)
        dry_run: If True, shows what would be exported without writing file
    
    Returns:
        Dictionary with export results:
        {
            'success': bool,
            'records_exported': int,
            'output_file': str,
            'message': str,
        }
    
    Raises:
        FileNotFoundError: If output directory doesn't exist
        PermissionError: If unable to write to output directory
    """
    try:
        # In real implementation:
        # from kinetic_devops.auth import KineticConfigManager
        # from kinetic_devops.baq import BAQClient
        # config = KineticConfigManager()
        # client = BAQClient(config)
        # customers = client.execute_baq('CUST_List', args={'MaxResults': limit})
        
        # Mock data for example
        customers = MOCK_CUSTOMERS[:limit] if limit else MOCK_CUSTOMERS
        
        if dry_run:
            return {
                'success': True,
                'records_exported': len(customers),
                'output_file': output_file,
                'message': f'DRY RUN: Would export {len(customers)} customers',
                'preview': customers[:3],
            }
        
        # Write to file
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(customers, f, indent=2, ensure_ascii=False)
        
        return {
            'success': True,
            'records_exported': len(customers),
            'output_file': output_file,
            'message': f'Successfully exported {len(customers)} customers',
        }
    
    except Exception as e:
        return {
            'success': False,
            'records_exported': 0,
            'output_file': output_file,
            'message': f'Export failed: {str(e)}',
            'error': str(e),
        }


def get_customer_count() -> Dict[str, Any]:
    """
    Get count of customers in the system.
    
    Returns:
        Dictionary with customer count
    """
    # Mock implementation
    return {
        'success': True,
        'customer_count': len(MOCK_CUSTOMERS),
        'message': f'Total customers: {len(MOCK_CUSTOMERS)}',
    }


def validate_customer_data() -> Dict[str, Any]:
    """
    Validate customer data for common issues.
    
    Returns:
        Dictionary with validation results
    """
    issues = []
    
    for cust in MOCK_CUSTOMERS:
        if not cust.get('CustomerID'):
            issues.append(f"Missing CustomerID in {cust}")
        if not cust.get('CustomerName'):
            issues.append(f"Missing CustomerName for {cust.get('CustomerID')}")
    
    return {
        'success': len(issues) == 0,
        'total_records': len(MOCK_CUSTOMERS),
        'issues_found': len(issues),
        'issues': issues,
        'message': f'Validation complete: {len(issues)} issues found',
    }
