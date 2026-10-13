# Quick Start Guide

Get up and running with the kinetic-devops project template in 5 minutes.

## 1. Clone the Template

```bash
# Copy from kinetic-devops examples
cp -r ../examples/project_template my_kinetic_project
cd my_kinetic_project
```

## 2. Install Dependencies

```bash
# Install kinetic-devops and template requirements
pip install -r requirements.txt
```

## 3. Explore the Loader

```bash
# List all available functions
python loader.py --list

# See function details
python loader.py --info functions.pull_customers

# Validate functions are properly documented
python loader.py --validate
```

## 4. Run an Example Function

```bash
# Run the customer exporter
python loader.py --run functions.pull_customers.export_customers

# Run with custom arguments
python loader.py --run functions.pull_customers.export_customers \
  --args '{"output_file": "my_customers.json", "limit": 100}'
```

## 5. Create Your First Function

```bash
# Create a new function file
cat > functions/my_first_function.py << 'EOF'
"""
My first custom function.

A simple example showing how to create self-documenting functions.
"""

__metadata__ = {
    'name': 'My First Function',
    'version': '1.0.0',
    'author': 'Your Name',
    'tags': ['example', 'custom'],
}

def hello_world(name: str = 'Developer') -> dict:
    """Say hello to someone."""
    return {
        'success': True,
        'message': f'Hello, {name}!',
    }
EOF
```

## 6. Run Your Function

```bash
# Discover it
python loader.py --list | grep "My First"

# Run it
python loader.py --run functions.my_first_function.hello_world

# Run with arguments
python loader.py --run functions.my_first_function.hello_world \
  --args '{"name": "Alice"}'
```

## Next Steps

### Organization Patterns

**Single Function:**
```
functions/my_utility.py    # One self-contained function
```

**Client-Specific Code:**
```
layers/CLIENT_A/custom.py  # CLIENT_A specific customization
layers/CLIENT_B/custom.py  # CLIENT_B specific customization
```

**Complex Feature:**
```
functions/
├── data_migration/
│   ├── __init__.py
│   ├── extract.py
│   ├── transform.py
│   └── load.py
```

### Common Tasks

**Add a new layer for a client:**
```bash
mkdir -p layers/MY_CLIENT
cat > layers/MY_CLIENT/__init__.py << 'EOF'
"""MY_CLIENT customizations."""
EOF
```

**Create a scheduled job:**
```bash
mkdir -p jobs/weekly
cat > jobs/weekly/my_job.py << 'EOF'
__metadata__ = {
    'name': 'My Weekly Job',
    'schedule': '0 0 * * 0',  # Sunday midnight
    'tags': ['job', 'weekly'],
}
EOF
```

**Create a BPM workflow:**
See `BPM/po_approval.py` for a complete approval workflow example.

**Create a data export app:**
See `apps/sales_reporting.py` for a reporting application example.

### Key Concepts

- **Metadata:** Every module should have `__metadata__` dict (see examples)
- **Discovery:** Loader automatically finds and catalogs functions
- **Filtering:** Use --type, --tag, --search flags to find functions
- **Validation:** Run `loader.py --validate` to check completeness
- **Composition:** Combine multiple functions into higher-level apps

### Real-World Example

```python
# functions/daily_report.py

__metadata__ = {
    'name': 'Daily Sales Report',
    'version': '1.0.0',
    'tags': ['report', 'sales', 'daily'],
}

def generate_report(date: str) -> dict:
    """Generate daily sales report."""
    # Import other functions
    from functions.pull_customers import export_customers
    from functions.validate_data import validate_environment
    
    # Validate data first
    validation = validate_environment(environment='production')
    
    if not validation['success']:
        return {'success': False, 'error': 'Validation failed'}
    
    # Export customers
    export = export_customers(output_file=f'report_{date}.json')
    
    return {
        'success': True,
        'report_date': date,
        'validation': validation,
        'export': export,
    }
```

## Troubleshooting

### Function not found
```bash
# Check discovery
python loader.py --list --search "keyword"

# Validate syntax
python loader.py --validate
```

### Import errors
```bash
# Make sure kinetic-devops is installed
pip install kinetic-devops

# Check your PYTHONPATH
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### Missing metadata
All modules need a `__metadata__` dict:
```python
__metadata__ = {
    'name': 'Function Name',
    'version': '1.0.0',
    'tags': ['tag1', 'tag2'],
}
```

## Documentation

- [Full README](README.md) - Complete guide to project structure
- [Loader Options](loader.py) - All loader command-line options
- [Examples](.) - Real-world examples included

## Support

See the kinetic-devops documentation for:
- How to use BAQ client: [kinetic_devops/baq.py](../../kinetic_devops/baq.py)
- How to use auth: [kinetic_devops/auth.py](../../kinetic_devops/auth.py)
- Base client details: [kinetic_devops/base_client.py](../../kinetic_devops/base_client.py)

Happy coding! 🚀
