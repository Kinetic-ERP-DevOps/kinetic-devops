#!/usr/bin/env python3
"""
Project Loader - Auto-discovery and execution of project functions.

This script scans the project structure and automatically discovers,
catalogs, and executes functions from folders (functions/, apps/, 
layers/, BPM/, jobs/).

Each module should include a __metadata__ dict with:
- name: Human-readable name
- version: Semantic version
- tags: List of tags for filtering
- dependencies: Required imports
"""

import os
import sys
import json
import importlib.util
import inspect
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class FunctionMetadata:
    """Metadata for a discoverable function."""
    name: str
    module_path: str
    function_name: Optional[str] = None
    version: str = "1.0.0"
    author: str = "Unknown"
    tags: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    description: str = ""
    usage_example: str = ""
    category: str = "functions"  # functions, apps, layers, BPM, jobs


class ProjectLoader:
    """Dynamically loads and manages project functions."""
    
    def __init__(self, project_root: Optional[str] = None):
        """Initialize loader with project root directory."""
        self.project_root = Path(project_root or os.getcwd())
        self.functions: Dict[str, FunctionMetadata] = {}
        self.categories = ['functions', 'apps', 'layers', 'BPM', 'jobs']
        self._discover_functions()
    
    def _discover_functions(self) -> None:
        """Scan project structure and discover all functions."""
        for category in self.categories:
            category_path = self.project_root / category
            if not category_path.exists():
                continue
            
            self._scan_directory(category_path, category)
    
    def _scan_directory(self, directory: Path, category: str, prefix: str = "") -> None:
        """Recursively scan directory for Python modules."""
        if not directory.is_dir():
            return
        
        for item in directory.rglob("*.py"):
            if item.name.startswith("_"):
                continue
            
            try:
                self._load_module(item, category)
            except Exception as e:
                print(f"Warning: Failed to load {item}: {e}", file=sys.stderr)
    
    def _load_module(self, module_path: Path, category: str) -> None:
        """Load a Python module and extract metadata."""
        rel_path = module_path.relative_to(self.project_root)
        module_name = str(rel_path).replace(os.sep, ".").replace(".py", "")
        
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                return
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # Extract metadata from module
            if hasattr(module, '__metadata__'):
                metadata_dict = module.__metadata__
                metadata = FunctionMetadata(
                    name=metadata_dict.get('name', module_name),
                    module_path=module_name,
                    version=metadata_dict.get('version', '1.0.0'),
                    author=metadata_dict.get('author', 'Unknown'),
                    tags=metadata_dict.get('tags', []),
                    dependencies=metadata_dict.get('dependencies', []),
                    description=inspect.getdoc(module) or "",
                    usage_example=metadata_dict.get('usage_example', ""),
                    category=category,
                )
                self.functions[module_name] = metadata
                
                # If module has executable functions, list them too
                for name, obj in inspect.getmembers(module):
                    if (inspect.isfunction(obj) and 
                        not name.startswith("_") and
                        obj.__module__ == module_name):
                        
                        func_key = f"{module_name}.{name}"
                        func_metadata = FunctionMetadata(
                            name=f"{metadata.name} › {name}",
                            module_path=module_name,
                            function_name=name,
                            version=metadata.version,
                            author=metadata.author,
                            tags=metadata.tags,
                            dependencies=metadata.dependencies,
                            description=inspect.getdoc(obj) or "",
                            category=category,
                        )
                        self.functions[func_key] = func_metadata
        
        except Exception as e:
            print(f"Warning: Error loading {module_path}: {e}", file=sys.stderr)
    
    def list_functions(
        self, 
        category: Optional[str] = None,
        tag: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[FunctionMetadata]:
        """List all discovered functions with optional filtering."""
        results = list(self.functions.values())
        
        if category:
            results = [f for f in results if f.category == category]
        
        if tag:
            results = [f for f in results if tag in f.tags]
        
        if search:
            search_lower = search.lower()
            results = [f for f in results 
                      if search_lower in f.name.lower() or
                         search_lower in f.description.lower()]
        
        return sorted(results, key=lambda x: x.name)
    
    def get_function(self, module_path: str, function_name: Optional[str] = None) -> Optional[Callable]:
        """Load and return a callable function."""
        try:
            spec = importlib.util.find_spec(module_path)
            if spec is None or spec.loader is None:
                return None
            
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            
            if function_name:
                return getattr(module, function_name, None)
            else:
                return module
        except Exception as e:
            print(f"Error loading {module_path}: {e}", file=sys.stderr)
            return None
    
    def validate(self) -> Dict[str, Any]:
        """Validate all functions for completeness."""
        results = {
            'total': len(self.functions),
            'valid': 0,
            'warnings': [],
            'errors': [],
        }
        
        for path, metadata in self.functions.items():
            issues = []
            
            # Check for required metadata
            if not metadata.name or metadata.name == path:
                issues.append("missing or default 'name' in __metadata__")
            
            if not metadata.version:
                issues.append("missing 'version' in __metadata__")
            
            if not metadata.tags:
                issues.append("missing 'tags' in __metadata__")
            
            if not metadata.description:
                issues.append("missing module docstring")
            
            if issues:
                results['warnings'].append({
                    'function': path,
                    'issues': issues,
                })
            else:
                results['valid'] += 1
        
        return results


def format_metadata(metadata: FunctionMetadata) -> str:
    """Format metadata for display."""
    lines = [
        f"\n📦 {metadata.name}",
        f"   Module: {metadata.category}/{metadata.module_path}",
        f"   Version: {metadata.version}",
        f"   Author: {metadata.author}",
    ]
    
    if metadata.function_name:
        lines.append(f"   Function: {metadata.function_name}()")
    
    if metadata.tags:
        lines.append(f"   Tags: {', '.join(metadata.tags)}")
    
    if metadata.dependencies:
        lines.append(f"   Dependencies: {', '.join(metadata.dependencies)}")
    
    if metadata.description:
        lines.append(f"\n   {metadata.description}")
    
    if metadata.usage_example:
        lines.append(f"\n   Usage:\n   {metadata.usage_example}")
    
    return "\n".join(lines)


def main():
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        description="Project loader - discover and execute project functions"
    )
    
    parser.add_argument(
        '--list',
        action='store_true',
        help='List all discovered functions'
    )
    
    parser.add_argument(
        '--type',
        choices=['functions', 'apps', 'layers', 'BPM', 'jobs'],
        help='Filter by function type/category'
    )
    
    parser.add_argument(
        '--tag',
        help='Filter by tag'
    )
    
    parser.add_argument(
        '--search',
        help='Search by name or description'
    )
    
    parser.add_argument(
        '--info',
        help='Show detailed info about a function'
    )
    
    parser.add_argument(
        '--run',
        help='Run a specific function (module.function format)'
    )
    
    parser.add_argument(
        '--args',
        help='JSON arguments to pass to function'
    )
    
    parser.add_argument(
        '--validate',
        action='store_true',
        help='Validate all functions for completeness'
    )
    
    parser.add_argument(
        '--project-root',
        default=os.getcwd(),
        help='Project root directory (default: current directory)'
    )
    
    args = parser.parse_args()
    
    # Initialize loader
    loader = ProjectLoader(args.project_root)
    
    # Handle commands
    if args.list:
        functions = loader.list_functions(
            category=args.type,
            tag=args.tag,
            search=args.search
        )
        
        if not functions:
            print("No functions found matching criteria.")
            return
        
        print(f"\n📚 Found {len(functions)} functions:\n")
        
        for func in functions:
            category_emoji = {
                'functions': '⚙️',
                'apps': '📱',
                'layers': '📊',
                'BPM': '⚙️',
                'jobs': '⏰',
            }.get(func.category, '📦')
            
            func_indicator = f" › {func.function_name}()" if func.function_name else ""
            tags_str = f" [{', '.join(func.tags)}]" if func.tags else ""
            
            print(f"{category_emoji} {func.name}{func_indicator}{tags_str}")
            print(f"   {func.module_path} (v{func.version})")
            if func.description:
                desc_first_line = func.description.split('\n')[0]
                print(f"   {desc_first_line}")
            print()
    
    elif args.info:
        if args.info in loader.functions:
            metadata = loader.functions[args.info]
            print(format_metadata(metadata))
        else:
            print(f"Function '{args.info}' not found.")
            print("\nDid you mean?")
            similar = loader.list_functions(search=args.info.split('.')[-1])
            for func in similar[:5]:
                print(f"  - {func.module_path}")
    
    elif args.run:
        parts = args.run.rsplit('.', 1)
        
        if len(parts) == 2:
            module_path, func_name = parts
            func = loader.get_function(module_path, func_name)
        else:
            func = loader.get_function(args.run)
        
        if func is None:
            print(f"Could not load function: {args.run}")
            return
        
        # Parse arguments if provided
        try:
            func_args = json.loads(args.args) if args.args else {}
        except json.JSONDecodeError:
            print(f"Invalid JSON arguments: {args.args}")
            return
        
        # Execute function
        try:
            print(f"\n▶️  Executing: {args.run}")
            print(f"   Arguments: {json.dumps(func_args, indent=2)}\n")
            
            start_time = datetime.now()
            result = func(**func_args)
            duration = (datetime.now() - start_time).total_seconds()
            
            print(f"\n✅ Execution completed in {duration:.2f}s")
            print(f"   Result: {json.dumps(result, indent=2, default=str)}")
        
        except Exception as e:
            print(f"\n❌ Execution failed:")
            print(f"   {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    
    elif args.validate:
        results = loader.validate()
        
        print(f"\n✓ Validation Results")
        print(f"  Total functions: {results['total']}")
        print(f"  Valid: {results['valid']}")
        print(f"  Warnings: {len(results['warnings'])}")
        
        if results['warnings']:
            print(f"\n⚠️  Issues found:\n")
            for warning in results['warnings']:
                print(f"  {warning['function']}:")
                for issue in warning['issues']:
                    print(f"    - {issue}")
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
