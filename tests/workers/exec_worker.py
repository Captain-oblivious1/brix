"""A generic protocol worker for tests: runs each request's args as a
subprocess in the request's cwd and returns the result. Because it just
execs the argv, a worker-routed action must produce exactly what spawning
would — that is the property the worker tests assert.
"""

import json
import subprocess
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    proc = subprocess.run(req["args"], cwd=req["cwd"], capture_output=True, text=True)
    sys.stdout.write(
        json.dumps(
            {
                "id": req["id"],
                "exitCode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
        + "\n"
    )
    sys.stdout.flush()
