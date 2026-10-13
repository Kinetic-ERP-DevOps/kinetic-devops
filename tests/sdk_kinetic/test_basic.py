import unittest
from kinetic_devops import KineticFileService


class TestKineticDKBasic(unittest.TestCase):
    def test_file_service_class_exists(self):
        # Basic smoke test: class is importable
        self.assertTrue(callable(KineticFileService))


if __name__ == '__main__':
    unittest.main()
