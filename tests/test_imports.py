"""
tests/test_imports.py - Test service imports and basic functionality.
"""
import unittest
from kinetic_devops import (
    KineticBAQService,
    KineticBOReaderService,
    KineticFileService,
    KineticConfigManager,
    KineticBaseClient,
)


class TestServiceImports(unittest.TestCase):
    """Verify all services can be imported."""
    
    def test_baq_service_import(self):
        """Test KineticBAQService imports."""
        self.assertTrue(callable(KineticBAQService))
        self.assertTrue(hasattr(KineticBAQService, 'get_baq_results'))
    
    def test_boreader_service_import(self):
        """Test KineticBOReaderService imports."""
        self.assertTrue(callable(KineticBOReaderService))
        self.assertTrue(hasattr(KineticBOReaderService, 'get_list'))
    
    def test_file_service_import(self):
        """Test KineticFileService imports."""
        self.assertTrue(callable(KineticFileService))
        self.assertTrue(hasattr(KineticFileService, 'get_dms_storage_types'))
        self.assertTrue(hasattr(KineticFileService, 'update_dms_storage_type'))
        self.assertTrue(hasattr(KineticFileService, 'set_default_storage_type'))
        self.assertTrue(hasattr(KineticFileService, 'get_file_service_status'))
    
    def test_base_client_import(self):
        """Test KineticBaseClient imports."""
        self.assertTrue(callable(KineticBaseClient))
        self.assertTrue(hasattr(KineticBaseClient, 'execute_request'))
        self.assertTrue(hasattr(KineticBaseClient, 'build_headers'))
    
    def test_config_manager_import(self):
        """Test KineticConfigManager imports."""
        self.assertTrue(callable(KineticConfigManager))


class TestServiceMethods(unittest.TestCase):
    """Verify service methods are callable."""
    
    def test_baq_methods_callable(self):
        """Verify BAQ service methods are callable."""
        self.assertTrue(callable(KineticBAQService.get_baq_results))
    
    def test_file_service_methods_callable(self):
        """Verify File service methods are callable."""
        self.assertTrue(callable(KineticFileService.get_dms_storage_types))
        self.assertTrue(callable(KineticFileService.update_dms_storage_type))
        self.assertTrue(callable(KineticFileService.update_dms_storage_types))
        self.assertTrue(callable(KineticFileService.set_default_storage_type))
        self.assertTrue(callable(KineticFileService.get_file_service_status))
    
    def test_boreader_methods_callable(self):
        """Verify BOReader service methods are callable."""
        self.assertTrue(callable(KineticBOReaderService.get_list))


if __name__ == '__main__':
    unittest.main()
