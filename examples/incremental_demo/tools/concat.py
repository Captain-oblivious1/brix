import sys
from pathlib import Path

Path("runs.log").open("a").write("combine\n")
parts = [Path(p).read_text() for p in sys.argv[1:-1]]
Path(sys.argv[-1]).write_text("".join(parts))
print(f"combined {len(sys.argv) - 2} file(s) -> {sys.argv[-1]}")
