import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtnhmod.config import Config
from gtnhmod import wiki

class TestBrowserVerification(unittest.TestCase):
    def test_find_system_browser(self):
        from gtnhmod.wiki import _find_system_browser
        browser = _find_system_browser()
        self.assertTrue(browser is None or Path(browser).exists())

if __name__ == "__main__":
    unittest.main()
