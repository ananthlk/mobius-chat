"""A name used before it is assigned, in a 6,900-line function, is silent.

THREE TIMES IN THE SAME BLOCK:
  1. `_pp_time_mod`, bound ~100 lines below the preload block -- recorded in
     a comment there.
  2. the `kept` work, earlier on 2026-09-13.
  3. `ctx._v2_preload_round = rn` -- preload runs BEFORE the round loop, so
     `rn` does not exist. The fail-soft swallowed the UnboundLocalError,
     logged "[v2.preload] failed", and EVERY v2 turn ran with no preloaded
     evidence while the trace still read "Looking this up before I answer".
     Ananth caught it from the trace, not from any test.

Python raises UnboundLocalError only when the line RUNS, and this one runs
inside a try/except that must stay fail-soft -- so no unit test of preload
would have failed. This is a static check: inside run_react, a local read
before its first assignment is a defect, whatever the branch.
"""
import ast
import inspect


def _run_react_ast():
    import app.pipeline.react_loop as RL
    src = inspect.getsource(RL)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_react":
            return node
    raise AssertionError("run_react not found — this guard is pointed at nothing")


def test_run_react_never_reads_a_local_before_assigning_it():
    fn = _run_react_ast()

    # Every name assigned anywhere in the function body, with the line it is
    # FIRST bound on. Anything read above that line is a forward reference.
    first_bind: dict[str, int] = {}

    def _bind(name, lineno):
        if name not in first_bind or lineno < first_bind[name]:
            first_bind[name] = lineno

    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            _bind(n.id, n.lineno)
        elif isinstance(n, ast.For):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    _bind(t.id, getattr(t, "lineno", 0))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            _bind(n.name, n.lineno)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                _bind((a.asname or a.name).split(".")[0], n.lineno)
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            for t in ast.walk(n.optional_vars):
                if isinstance(t, ast.Name):
                    _bind(t.id, getattr(t, "lineno", 0))

    # 🔴 COMPREHENSION TARGETS ARE THEIR OWN SCOPE (Python 3). `_c` inside a
    # listcomp at line 7068 is NOT the `_c` bound by a different comp at 7087.
    # My first version of this guard treated them as function locals and
    # reported four offenders against correct code -- a guard that cries wolf
    # gets deleted, and then the real defect has nothing watching it.
    # Also nested def/lambda PARAMETERS: `def _ident(_c)` and
    # `lambda _t, _i:` bind their own _c/_t/_i, unrelated to any outer name.
    comp_names: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp,
                          ast.GeneratorExp)):
            for gen in n.generators:
                for t in ast.walk(gen.target):
                    if isinstance(t, ast.Name):
                        comp_names.add(t.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if n is fn:
                continue
            a = n.args
            for arg in (a.args + a.kwonlyargs + a.posonlyargs):
                comp_names.add(arg.arg)
            if a.vararg:
                comp_names.add(a.vararg.arg)
            if a.kwarg:
                comp_names.add(a.kwarg.arg)

    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    if fn.args.vararg:
        params.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)

    offenders = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            continue
        if n.id in params or n.id in comp_names or n.id not in first_bind:
            continue          # a parameter, a global, or a builtin
        if n.lineno < first_bind[n.id]:
            offenders.append((n.id, n.lineno, first_bind[n.id]))

    assert not offenders, (
        "run_react reads a local before its first assignment — the exact "
        "defect that killed preload on every v2 turn:\n"
        + "\n".join(f"  {name!r} read at line {read}, first assigned at "
                    f"{bound}" for name, read, bound in sorted(offenders)[:10])
    )
