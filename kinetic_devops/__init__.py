# kinetic_devops/__init__.py
import importlib.util
import sys


def _lazy_import(name):
    """Return module `name`, deferring its actual import until an attribute
    is first used (importlib.util.LazyLoader). keyring alone costs seconds to
    import on some machines; this keeps paths that never touch it, like
    `--help`, from paying for it."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.find_spec(name)
    if spec is None or spec.loader is None:
        raise ImportError(f"No module named '{name}'")
    loader = importlib.util.LazyLoader(spec.loader)
    spec.loader = loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


# Check that keyring is installed without paying to import it.
if importlib.util.find_spec("keyring") is None:
    print("Error: The 'keyring' module is not installed. Please activate the virtual environment and install the required packages.")
    print("To activate the virtual environment, run: .\\.venv\\Scripts\\activate")
    print("To install the required packages, run: pip install -r requirements.txt")
    exit(1)

from importlib import import_module


_LAZY_ATTRS = {
    "KineticConfigManager": (".auth", "KineticConfigManager"),
    "KineticBaseClient": (".base_client", "KineticBaseClient"),
    "KineticMetafetcher": (".metafx", "KineticMetafetcher"),
    "KineticBAQService": (".baq", "KineticBAQService"),
    "KineticBOReaderService": (".boreader", "KineticBOReaderService"),
    "KineticFileService": (".file_service", "KineticFileService"),
    "KineticReportService": (".report_service", "KineticReportService"),
    "KineticExportAllService": (".export_all", "KineticExportAllService"),
    "KineticSolutionService": (".solutions", "KineticSolutionService"),
    "KineticZDataTableService": (".zdatatable", "KineticZDataTableService"),
}


def __getattr__(name):
    if name == "find_sensitive_data":
        module = import_module(".find_sensitive_data", __name__)
        globals()[name] = module
        return module

    target = _LAZY_ATTRS.get(name)
    if target is not None:
        module_name, attr_name = target
        module = import_module(module_name, __name__)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value

    raise AttributeError(f"module 'kinetic_devops' has no attribute '{name}'")

__all__ = [
    "KineticConfigManager",
    "KineticBaseClient",
    "KineticMetafetcher",
    "KineticBAQService",
    "KineticBOReaderService",
    "KineticFileService",
    "KineticReportService",
    "KineticExportAllService",
    "KineticSolutionService",
    "KineticZDataTableService",
    "find_sensitive_data",
]