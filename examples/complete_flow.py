"""
Complete example: Auth, Connection, and BAQ Execution.

This is a real, working example showing the complete flow of:
1. Establishing authentication via KineticConfigManager
2. Creating a BAQ client with proper session management
3. Executing a BAQ query
4. Processing and exporting results

This is the "hello world" example that demonstrates the complete pattern
for connecting to Epicor Kinetic and pulling data.

Usage:
    python loader.py --run examples.complete_flow.execute_customer_baq
    
Or in Python:
    from examples.complete_flow import execute_customer_baq
    results = execute_customer_baq(company='ABC', limit=100)
"""

__metadata__ = {
    'name': 'Complete Auth & BAQ Flow',
    'version': '1.0.0',
    'author': 'Kinetic Team',
    'tags': ['example', 'auth', 'baq', 'complete-flow', 'getting-started'],
    'dependencies': ['kinetic_devops.auth', 'kinetic_devops.baq'],
    'usage_example': '''
from examples.complete_flow import execute_customer_baq

# Execute a BAQ query and get results
results = execute_customer_baq(
    company='ABC',
    limit=100,
    output_file='customers.json'
)

print(f"Retrieved {results['record_count']} customers")
    ''',
}

import json
from typing import Dict, List, Optional, Any


def execute_customer_baq(
    company: str = 'ABC',
    limit: Optional[int] = None,
    output_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Complete example: Authenticate, connect, and execute a BAQ.
    
    This function demonstrates the full pattern:
    
    Step 1: Get authentication credentials via KineticConfigManager
    Step 2: Create a BAQ client with authenticated session
    Step 3: Execute a BAQ query (CUST_List for customers)
    Step 4: Process results and optionally export to file
    
    Args:
        company: Company code (e.g., 'ABC', 'XYZ')
        limit: Max records to retrieve (None = all, default 10,000)
        output_file: Optional JSON file to export results
    
    Returns:
        Dictionary with execution results:
        {
            'success': bool,
            'record_count': int,
            'records': List[Dict],
            'output_file': Optional[str],
            'execution_time': float,
        }
    
    Real Implementation (step by step):
    
        # STEP 1: Initialize authentication
        from kinetic_devops.auth import KineticConfigManager
        config = KineticConfigManager()
        
        # This prompts user for credentials if not stored
        # Uses keyring for secure credential storage
        config.prompt_for_env()
        
        # STEP 2: Create BAQ client
        from kinetic_devops.baq import BAQClient
        baq_client = BAQClient(config)
        
        # The BaseClient handles:
        # - Session management
        # - Token refresh
        # - Request signing
        # - Response handling
        
        # STEP 3: Execute the BAQ
        results = baq_client.execute_baq(
            'CUST_List',  # BAQ name
            args={
                'Company': company,
                'MaxResults': limit or 10000,
            }
        )
        
        # STEP 4: Process results
        records = results.get('data', [])
        if output_file:
            with open(output_file, 'w') as f:
                json.dump(records, f, indent=2)
    """
    
    # ==========================================
    # STEP 1: AUTHENTICATE
    # ==========================================
    try:
        from kinetic_devops.auth import KineticConfigManager
        
        config = KineticConfigManager()
        
        # Get the active configuration (or prompt user)
        active_env = config.get_active_config()
        if not active_env:
            return {
                'success': False,
                'error': 'No authentication configured. Run: kinetic-devops auth setup',
                'instructions': 'Set up credentials with: python loader.py --run kinetic_devops.auth.main',
            }
    
    except ImportError as e:
        return {
            'success': False,
            'error': f'kinetic_devops not installed: {e}',
            'fix': 'pip install kinetic-devops',
        }
    
    # ==========================================
    # STEP 2: CREATE BAQ CLIENT
    # ==========================================
    try:
        from kinetic_devops.baq import BAQClient
        
        # BAQ client inherits from BaseClient which handles:
        # - Request authentication
        # - Token lifecycle
        # - Session management
        # - Wire logging (redacted for security)
        baq_client = BAQClient(config)
    
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to create BAQ client: {e}',
        }
    
    # ==========================================
    # STEP 3: EXECUTE BAQ QUERY
    # ==========================================
    try:
        # Execute the customer list BAQ
        response = baq_client.execute_baq(
            baq_id='CUST_List',
            args={
                'Company': company,
                'MaxResults': limit or 10000,
            }
        )
        
        records = response.get('value', [])
        
        if not response.get('success', True):
            return {
                'success': False,
                'error': response.get('message', 'BAQ execution failed'),
            }
    
    except Exception as e:
        return {
            'success': False,
            'error': f'BAQ execution failed: {e}',
        }
    
    # ==========================================
    # STEP 4: PROCESS AND EXPORT RESULTS
    # ==========================================
    try:
        export_path = None
        
        if output_file and records:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(records, f, indent=2, default=str)
            export_path = output_file
        
        return {
            'success': True,
            'company': company,
            'record_count': len(records),
            'records': records[:5] if records else [],  # Return first 5 as preview
            'total_records': len(records),
            'output_file': export_path,
            'message': f'Retrieved {len(records)} customer records',
            'next_steps': [
                'Export data to file: output_file parameter',
                'Filter results: Add where clauses to BAQ args',
                'Transform data: Process records in Step 4',
            ],
        }
    
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to process results: {e}',
        }


def list_available_baqs() -> Dict[str, Any]:
    """
    List available BAQs in your Epicor Kinetic system.
    
    Requires authentication to be configured first.
    """
    return {
        'success': True,
        'note': 'This requires real Epicor Kinetic connection',
        'common_baqs': [
            'CUST_List - All customers',
            'VENDOR_List - All vendors',
            'PO_List - All purchase orders',
            'SO_List - All sales orders',
            'PART_List - All parts/inventory',
            'EMP_List - All employees',
        ],
        'documentation': 'See Epicor Kinetic Dashboard > System Manager > BAQs',
    }


def execute_custom_baq(
    baq_id: str,
    company: str = 'ABC',
    **kwargs
) -> Dict[str, Any]:
    """
    Generic BAQ executor for any BAQ.
    
    Args:
        baq_id: BAQ identifier (e.g., 'CUST_List', 'PO_List')
        company: Company code
        **kwargs: Any additional BAQ parameters
    
    Example:
        # Execute PO list BAQ with filters
        results = execute_custom_baq(
            'PO_List',
            company='ABC',
            Status='Open',
            MinAmount=1000,
        )
    """
    try:
        from kinetic_devops.auth import KineticConfigManager
        from kinetic_devops.baq import BAQClient
        
        config = KineticConfigManager()
        client = BAQClient(config)
        
        args = {'Company': company}
        args.update(kwargs)
        
        response = client.execute_baq(baq_id, args=args)
        
        return {
            'success': True,
            'baq_id': baq_id,
            'record_count': len(response.get('value', [])),
            'records': response.get('value', []),
        }
    
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }


# ============================================================
# PATTERN REFERENCE
# ============================================================
"""
The pattern shown above is the foundation for all kinetic-devops usage:

1. AUTHENTICATE
   from kinetic_devops.auth import KineticConfigManager
   config = KineticConfigManager()

2. CREATE CLIENT
   from kinetic_devops.baq import BAQClient
   client = BAQClient(config)

3. EXECUTE OPERATION
   results = client.execute_baq(baq_id, args={...})

4. PROCESS RESULTS
   records = results.get('value', [])
   # Export, transform, validate, etc.

This pattern is consistent across all kinetic-devops service clients:
- BAQClient (this example)
- ReportClient
- TaxClient
- FileServiceClient
- BOReaderClient

For Details:
- See kinetic_devops/auth.py for authentication patterns
- See kinetic_devops/baq.py for BAQ-specific usage
- See kinetic_devops/base_client.py for client architecture
"""
