# conftest.py  (project root, next to app/)

import sys
import os

# Add project root to Python path so 'app' is importable
sys.path.insert(0, os.path.dirname(__file__))