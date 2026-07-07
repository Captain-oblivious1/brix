"""A worker that reads one request and dies without answering — to exercise
the crash path (the in-flight request must fail with exit_code=None)."""

import sys

sys.stdin.readline()
sys.exit(3)
