#!/usr/bin/env python3
"""Fill an @token@ template — jackson's PackageVersion.java.in -> .java.

    gen_version.py <template> <output> key=value ...

brix runs commands without a shell (no redirection), so a generated source
is produced by a tool action like this one, whose output feeds the compile.
"""
import os
import sys

template, output = sys.argv[1], sys.argv[2]
subs = dict(arg.split("=", 1) for arg in sys.argv[3:])
text = open(template, encoding="utf-8").read()
for key, value in subs.items():
    text = text.replace(f"@{key}@", value)
os.makedirs(os.path.dirname(output), exist_ok=True)
open(output, "w", encoding="utf-8").write(text)
