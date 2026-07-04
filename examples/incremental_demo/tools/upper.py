import sys
from pathlib import Path

Path("runs.log").open("a").write(f"upper {sys.argv[1]}\n")
text = Path(sys.argv[1]).read_text()
Path(sys.argv[2]).write_text(text.upper())
print(f"uppercased {sys.argv[1]} -> {sys.argv[2]}")
