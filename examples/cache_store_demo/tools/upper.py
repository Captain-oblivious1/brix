import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src) as f:
    text = f.read()
with open(dst, "w") as f:
    f.write(text.upper())
print(f"uppercased {src} -> {dst}")
