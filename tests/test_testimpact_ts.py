"""The TypeScript test-impact provider (design: docs/test-impact-selection.md).

The import parser and resolver are pure and checked directly; `view()` is checked
on real files on disk. The end-to-end equivalence claim (a selected vitest run
matches a full run) is in `test_testimpact_ts_oracle.py`.
"""

from brix.testimpact import SelectionInput, select_tests, _vocabulary
from brix.testimpact_ts import TsProvider, parse_imports, resolve_import


# --- import parsing ----------------------------------------------------------

def test_parses_the_common_import_forms():
    specs, dyn = parse_imports(
        "import { a } from './a';\n"
        "import './side-effect';\n"
        "export { x } from '../b/x';\n"
        "import type { T } from './types';\n"
    )
    assert specs == frozenset({"./a", "./side-effect", "../b/x", "./types"})
    assert not dyn


def test_a_literal_dynamic_import_is_an_edge_not_dynamic():
    specs, dyn = parse_imports("const m = await import('./plugin');\n")
    assert "./plugin" in specs
    assert not dyn  # the specifier is known -> resolvable, not a runtime escape


def test_a_computed_dynamic_import_marks_the_file_dynamic():
    specs, dyn = parse_imports("const m = await import(name);\nconst r = require(x);\n")
    assert dyn


# --- specifier resolution ----------------------------------------------------

KNOWN = frozenset({"src/a.ts", "src/util.ts", "src/dir/index.ts"})


def test_resolves_relative_with_extension_fallback():
    assert resolve_import("./util", "src/a.ts", KNOWN) == "src/util.ts"


def test_resolves_a_directory_to_its_index():
    assert resolve_import("./dir", "src/a.ts", KNOWN) == "src/dir/index.ts"


def test_resolves_parent_relative():
    assert resolve_import("../a", "src/dir/index.ts", KNOWN) == "src/a.ts"


def test_bare_specifier_is_external_none():
    assert resolve_import("vitest", "src/a.ts", KNOWN) is None


def test_unresolvable_relative_is_none():
    assert resolve_import("./missing", "src/a.ts", KNOWN) is None


# --- view() on real files ----------------------------------------------------

def _write(tmp_path, files: dict):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return list(files)


def _select(provider, changed):
    view = provider.view()
    return select_tests(SelectionInput(view, frozenset(changed), frozenset(_vocabulary(view))))


def test_view_builds_the_import_graph(tmp_path):
    files = _write(tmp_path, {
        "src/util.ts": "export const base = 10;\n",
        "src/adder.ts": "import { base } from './util';\nexport const add = (a:number,b:number)=>a+b;\n",
        "src/greeter.ts": "export const greet = (w:string)=>`hi ${w}`;\n",
        "test/adder.test.ts": "import { add } from '../src/adder';\ntest('x',()=>add(1,2));\n",
        "test/greeter.test.ts": "import { greet } from '../src/greeter';\ntest('x',()=>greet('w'));\n",
    })
    view = TsProvider(files=files, root=tmp_path).view()
    assert set(view.test_edges) == {"test/adder.test.ts", "test/greeter.test.ts"}
    assert view.code_deps["src/adder.ts"] == frozenset({"src/util.ts"})
    assert not view.uncovered and not view.dynamic_units
    assert view.binding_unit == "module"


def test_editing_a_shared_module_selects_only_dependent_tests(tmp_path):
    files = _write(tmp_path, {
        "src/util.ts": "export const base = 10;\n",
        "src/adder.ts": "import { base } from './util';\nexport const add = ()=>base;\n",
        "src/greeter.ts": "export const greet = ()=>'hi';\n",
        "test/adder.test.ts": "import { add } from '../src/adder';\ntest('x',()=>add());\n",
        "test/greeter.test.ts": "import { greet } from '../src/greeter';\ntest('x',()=>greet());\n",
    })
    provider = TsProvider(files=files, root=tmp_path)
    # util feeds adder feeds adder.test; greeter is independent.
    sel = _select(provider, {"src/util.ts"})
    assert not sel.run_all
    assert sel.tests == frozenset({"test/adder.test.ts"})


def test_a_computed_dynamic_import_makes_its_test_always_run(tmp_path):
    files = _write(tmp_path, {
        "src/loader.ts": "export async function load(n:string){ return import(n); }\n",
        "src/plain.ts": "export const x = 1;\n",
        "test/loader.test.ts": "import { load } from '../src/loader';\ntest('x',()=>load('a'));\n",
        "test/plain.test.ts": "import { x } from '../src/plain';\ntest('x',()=>x);\n",
    })
    provider = TsProvider(files=files, root=tmp_path)
    view = provider.view()
    assert "src/loader.ts" in view.dynamic_units
    # Editing something unrelated must still run the dynamic-reaching test.
    sel = _select(provider, {"src/plain.ts"})
    assert "test/loader.test.ts" in sel.tests   # always-run (reaches dynamic)
    assert "test/plain.test.ts" in sel.tests     # actually changed


def test_an_unfollowable_relative_import_fails_open(tmp_path):
    files = _write(tmp_path, {
        "src/a.ts": "import x from './generated/thing';\nexport const a = 1;\n",
        "test/a.test.ts": "import { a } from '../src/a';\ntest('x',()=>a);\n",
    })
    provider = TsProvider(files=files, root=tmp_path)
    view = provider.view()
    assert "src/a.ts" in view.uncovered  # unresolved relative -> incomplete graph
    sel = _select(provider, {"src/a.ts"})
    assert sel.run_all  # forces a full run
