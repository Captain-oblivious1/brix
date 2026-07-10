"""Suite-wide defaults.

Worker daemons off by default: without this, every worker test would leave
a brix-workerd holder (and its JVM/interpreter) idling for the 900-second
default after the suite exits. Tests that exercise the daemon itself pass
``worker_idle_timeout=`` explicitly, which takes precedence over the env
default (see ``engine._worker_daemon_config``).
"""

import os

os.environ.setdefault("BRIX_WORKER_IDLE_TIMEOUT", "0")
