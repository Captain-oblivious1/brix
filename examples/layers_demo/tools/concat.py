import sys

*srcs, dst = sys.argv[1:]
with open(dst, "w") as out:
    for src in srcs:
        with open(src) as f:
            out.write(f.read())
print(f"combined {len(srcs)} file(s) -> {dst}")
