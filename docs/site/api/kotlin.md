# Kotlin

Typed Kotlin actions — compilation through a warm `kotlinc` worker and jar
assembly — with the **kotlin-abi** lens (`abi`), which recovers the cross-module
compile-skip while staying sound about `inline` functions (whose bodies the
compiler copies into callers).

::: brix.kotlin
    options:
      show_root_heading: false
      show_root_toc_entry: false
