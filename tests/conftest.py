"""pytest configuration — adds project root to sys.path so all modules are importable."""

import sys
import os

# Ensure project root is always in sys.path, regardless of how tests are invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
