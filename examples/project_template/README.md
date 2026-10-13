# Kinetic DevOps Project Template

A structured project template for organizing your custom Epicor Kinetic integrations, automation scripts, and customizations on top of kinetic-devops.

## Folder Structure

```
my_project/
├── loader.py                    # Auto-discovery script (copy from template)
├── config.yaml                  # Project configuration
├── functions/                   # Reusable functions and utilities
│   ├── data_migration.py
│   ├── validation.py
│   └── helpers.py
├── layers/                      # DataView layers, customizations
│   ├── CLIENT_A/
│   │   └── dimension_layer.py
│   └── CLIENT_B/
│       └── custom_layer.py
├── apps/                        # Custom app builders and utilities
│   ├── CLIENT_A/
│   │   ├── dashboard.py
│   │   └── config.py
│   └── CLIENT_B/
│       └── reports.py
├── BPM/                         # Business Process Module customizations
│   ├── CLIENT_A/
│   │   ├── po_approval_bpm.py
│   │   └── quote_workflow_bpm.py
│   └── CLIENT_B/
│       └── invoice_bpm.py
└── jobs/                        # Scheduled automation jobs
    ├── daily/
    │   └── sync_companies.py
    ├── weekly/
    │   └── audit_log_cleanup.py
    └── monthly/
        └── tax_config_refresh.py
```

## How It Works

### 1. Self-Documenting Functions

Each function is a standard Python module with:
- A **module docstring** describing what it does
- A **metadata dict** with version, author, tags
- Clear function signatures with type hints

```python
"""
Sales Order data migration utility.

This module handles moving SO data between test and production environments,
with built-in validation and rollback capability.

Usage:
    from functions.data_migration import migrate_sales_orders
    results = migrate_sales_orders(from_env='test', to_env='prod')
"""

__metadata__ = {
    'name': 'Sales Order Migration',
    'version': '1.0.0',
    'author': 'Your Team',
    'tags': ['migration', 'sales', 'data'],
    'dependencies': ['kinetic_devops.baq', 'kinetic_devops.base_client'],
    'usage_example': 'python -m loader --list functions',
}

def migrate_sales_orders(from_env: str, to_env: str, dry_run: bool = True) -> dict:
    """Migrate sales orders between environments."""
    pass
```

### 2. Auto-Discovery Loader

The `loader.py` script automatically discovers and catalogs all functions:

```bash
# List all available functions
python loader.py --list

# List only migration functions
python loader.py --list --tag migration

# Execute a specific function
python loader.py --run functions.data_migration.migrate_sales_orders

# Execute with arguments
python loader.py --run functions.data_migration.migrate_sales_orders \
  --args '{"from_env": "test", "to_env": "prod", "dry_run": true}'

# Show detailed info about a function
python loader.py --info functions.data_migration
```

### 3. Organization Patterns

#### Single Function
```
functions/
└── data_migration.py    # Complete, self-contained functionality
```

#### Client-Specific Customizations
```
layers/
├── CLIENT_A/            # All CLIENT_A customizations here
│   ├── __init__.py
│   └── dimension_layer.py
├── CLIENT_B/
│   ├── __init__.py
│   └── custom_layer.py
```

#### Nested Folder Structure (Large Projects)
```
BPM/
├── company_a/
│   ├── po_approval/
│   │   ├── __init__.py
│   │   ├── validate.py
│   │   └── approve.py
│   └── project_sharing/
└── company_b/
    └── order_workflow/
```

## Getting Started

### 1. Copy the Template

```bash
# Copy template to create your project
cp -r examples/project_template my_kinetic_project
cd my_kinetic_project
```

### 2. Create Your First Function

```bash
# Create a simple data pulling function
cat > functions/pull_customers.py << 'EOF'
"""
Customer data extraction utility.

Pulls customer master data from Epicor Kinetic and exports to JSON.

Usage:
    from functions.pull_customers import export_customers
    export_customers(output_file='customers.json', limit=1000)
"""

__metadata__ = {
    'name': 'Customer Exporter',
    'version': '1.0.0',
    'author': 'Team',
    'tags': ['export', 'customers'],
}

import json
from kinetic_devops.baq import BAQClient
from kinetic_devops.auth import KineticConfigManager

def export_customers(output_file='customers.json', limit=None):
    """Export all customers to JSON file."""
    config = KineticConfigManager()
    client = BAQClient(config)
    
    # BAQ for customer export
    results = client.execute_baq(
        'CUST_List',
        args={'MaxResults': limit or 10000}
    )
    
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    
    return f"Exported {len(results)} customers"
EOF
```

### 3. List and Run Functions

```bash
# See all available functions
python loader.py --list

# Execute the customer export
python loader.py --run functions.pull_customers.export_customers
```

## Example Functions by Category

### Functions (Reusable Utilities)
- Data migration/ETL
- Validation helpers
- Report generators
- API wrappers
- Format converters

### Layers (Epicor Customizations)
- DataView layer customizations
- Business logic layers
- Data transformation rules
- Client-specific field mappings

### Apps (Kinetic App Building)
- Custom dashboard builders
- Report generators
- Form customizations
- Navigation helpers

### BPM (Workflow Automation)
- Purchase order approvals
- Quote-to-order workflows
- Invoice matching
- Inventory movements

### Jobs (Scheduled Automation)
- Daily: Company sync, data validation
- Weekly: Archive cleanup, report generation
- Monthly: Configuration refresh, audit rollups

## Running Functions

### Via Command Line

```bash
# List everything
python loader.py --list

# Filter by type
python loader.py --list --type functions
python loader.py --list --type jobs

# Filter by tag
python loader.py --list --tag migration

# Get info on a function
python loader.py --info functions.data_migration

# Execute a function
python loader.py --run functions.data_migration.migrate_sales_orders

# Execute with JSON args
python loader.py --run functions.data_migration.migrate_sales_orders \
  --args '{"from_env": "test", "to_env": "prod"}'
```

### Via Python Script

```python
from loader import ProjectLoader

loader = ProjectLoader()

# List all functions
all_functions = loader.list_functions()

# Get a specific function
func = loader.get_function('functions.pull_customers.export_customers')
result = func()

# Get functions by tag
migration_tools = loader.get_by_tag('migration')
```

## Best Practices

### 1. Self-Documenting Code
- Always include module docstrings with usage examples
- Use `__metadata__` dict for discoverability
- Use type hints in function signatures
- Include docstrings in functions

### 2. Naming Conventions
- **Functions**: `verb_noun.py` (e.g., `pull_customers.py`, `validate_data.py`)
- **Client folders**: `CLIENT_NAME/` (uppercase for clarity)
- **Function files**: snake_case for compatibility

### 3. Dependencies
- List required kinetic_devops modules in `__metadata__['dependencies']`
- Import at module level or function level based on use
- Use relative imports for internal functions

### 4. Error Handling
- Return consistent error structure: `{'success': False, 'error': 'msg'}`
- Log failures with context for debugging
- Include rollback capability where data is modified

### 5. Version Management
- Use semantic versioning in `__metadata__['version']`
- Update version when function changes
- Document breaking changes in docstring

## Real-World Examples

### Example 1: Data Validation Job

```python
"""
Nightly data validation job.

Validates company data integrity before daily operations.
Sends alerts if issues found.
"""

__metadata__ = {
    'name': 'Data Integrity Validator',
    'version': '2.1.0',
    'schedule': 'daily@02:00',  # 2 AM daily
    'tags': ['validation', 'nightly', 'critical'],
}

def validate_company_data(company_id: str) -> dict:
    """Check company data for consistency issues."""
    pass
```

### Example 2: Multi-Step Migration with Rollback

```python
"""
Full environment promotion script.

Promotes test configurations and data to production with
automatic rollback on validation failure.
"""

__metadata__ = {
    'name': 'Environment Promotion',
    'version': '3.0.0',
    'tags': ['migration', 'promotion', 'production'],
}

def promote_to_production(source: str, target: str) -> dict:
    """Promote environment with validation and rollback."""
    pass
```

### Example 3: Client-Specific Layer Customization

```python
# layers/CLIENT_A/dimension_layer.py

"""
Custom dimension layer for CLIENT_A.

Adds CLIENT_A-specific dimensions and hierarchies to
standard Epicor dimension definitions.

Company-dependent? Yes - CLIENT_A specific
"""

__metadata__ = {
    'name': 'CLIENT_A Dimension Layer',
    'client': 'CLIENT_A',
    'version': '1.5.0',
}

def apply_dimension_customizations(dim_code: str) -> dict:
    """Apply CLIENT_A customizations to dimension."""
    pass
```

## Integrating with CI/CD

### GitHub Actions Example

```yaml
# .github/workflows/validate.yml
name: Validate Project Functions

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
      - run: pip install -r requirements.txt
      - run: python loader.py --validate
      - run: python -m pytest tests/
```

### Makefile Example

```makefile
.PHONY: list run validate

list:
	python loader.py --list

run:
	python loader.py --run $(FUNCTION)

validate:
	python loader.py --validate

test:
	python -m pytest tests/
```

## Troubleshooting

### Function Not Found
```bash
python loader.py --validate  # Check for syntax errors
python loader.py --list      # Verify function is discovered
```

### Import Errors
Ensure `kinetic_devops` is installed:
```bash
pip install kinetic-devops
```

### Missing Metadata
All modules should have `__metadata__` dict:
```python
__metadata__ = {
    'name': 'Function Name',
    'version': '1.0.0',
}
```

## Next Steps

1. **Clone this template** to create your project
2. **Create your first function** in `functions/`
3. **Use the loader** to list and execute functions
4. **Organize by client** if managing multiple organizations
5. **Schedule jobs** for automation

See [kinetic-devops documentation](../../README.md) for platform features.
