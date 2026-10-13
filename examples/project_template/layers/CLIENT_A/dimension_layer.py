"""
CLIENT_A custom dimension layer.

Demonstrates how to create client-specific customizations using
kinetic-devops as a base. This layer adds CLIENT_A-specific
dimensions and hierarchies to standard Epicor definitions.

This is a typical use case for:
- Multi-tenant environments
- Company-specific customizations
- Layer-based architecture extending the platform

Organization pattern:
layers/
├── CLIENT_A/                    # Each client gets a folder
│   ├── __init__.py
│   ├── dimension_layer.py       # This file
│   └── hierarchy_definitions.py
└── CLIENT_B/
    └── ...
"""

__metadata__ = {
    'name': 'CLIENT_A Dimension Layer',
    'version': '2.1.0',
    'author': 'CLIENT_A Custom Dev Team',
    'tags': ['client-specific', 'dimension', 'customization', 'CLIENT_A'],
    'client': 'CLIENT_A',
    'dependencies': ['kinetic_devops.base_client'],
    'usage_example': '''
from layers.CLIENT_A.dimension_layer import get_custom_dimensions

dimensions = get_custom_dimensions(company_id='CLIENT_A', dim_code='CUSTGRP')
    ''',
}

from typing import Dict, List, Any, Optional


def get_custom_dimensions(
    company_id: str = 'CLIENT_A',
    dim_code: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get CLIENT_A-specific custom dimensions.
    
    Args:
        company_id: Company ID (should be CLIENT_A for this layer)
        dim_code: Specific dimension to retrieve (None = all)
    
    Returns:
        Dictionary of custom dimensions and properties
    """
    
    # CLIENT_A specific dimensions
    custom_dims = {
        'CUSTGRP': {
            'description': 'Customer Group (CLIENT_A specific)',
            'values': [
                'GROUP_A_PREMIUM',
                'GROUP_A_STANDARD',
                'GROUP_A_RETAIL',
                'GROUP_A_DISTRIBUTION',
            ],
        },
        'PROJPHASE': {
            'description': 'Project Phase (CLIENT_A extension)',
            'values': [
                'PLANNING',
                'REQUIREMENTS',
                'DESIGN',
                'DEVELOPMENT',
                'TESTING',
                'DEPLOYMENT',
            ],
        },
        'REVENUE_STREAM': {
            'description': 'Revenue stream categorization',
            'values': [
                'PRODUCT_SALES',
                'SERVICES',
                'SUPPORT',
                'LICENSING',
            ],
        },
    }
    
    if dim_code:
        return {
            'success': dim_code in custom_dims,
            'dimension': dim_code,
            'data': custom_dims.get(dim_code),
        }
    
    return {
        'success': True,
        'company': company_id,
        'dimensions': list(custom_dims.keys()),
        'data': custom_dims,
    }


def apply_dimension_customizations(dim_code: str, values: List[str]) -> Dict[str, Any]:
    """
    Apply CLIENT_A customizations to a dimension.
    
    Args:
        dim_code: Dimension code to customize
        values: New or updated values for the dimension
    
    Returns:
        Result of customization application
    """
    return {
        'success': True,
        'dimension': dim_code,
        'values_applied': len(values),
        'message': f'Applied {len(values)} customizations to {dim_code}',
        'values': values,
    }


def get_hierarchy_rules() -> Dict[str, Any]:
    """
    Get CLIENT_A-specific hierarchy rules for dimensions.
    
    CLIENT_A requires specific hierarchical relationships
    for reporting and analysis.
    """
    return {
        'success': True,
        'hierarchies': {
            'CUSTGRP_HIERARCHY': {
                'parent_dim': 'CUSTGRP',
                'levels': [
                    'GLOBAL',
                    'REGION',
                    'TERRITORY',
                    'GROUP',
                ],
            },
            'REVENUE_HIERARCHY': {
                'parent_dim': 'REVENUE_STREAM',
                'levels': [
                    'COMPANY',
                    'DIVISION',
                    'DEPARTMENT',
                    'STREAM',
                ],
            },
        },
        'message': 'Hierarchies defined for CLIENT_A',
    }
