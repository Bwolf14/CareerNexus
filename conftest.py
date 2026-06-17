"""Make the repo root importable so ``import resume_parser`` works under pytest."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
