"""The mcp SDK renamed a family of attributes at 2.0, and one rename is silent.

    1.26                      2.x                       how it fails
    streamablehttp_client  -> streamable_http_client     ImportError, loud
    Tool.inputSchema       -> Tool.input_schema          AttributeError, loud
    CallToolResult.isError -> CallToolResult.is_error    getattr(...) -> False

The third is the dangerous one BECAUSE it was already read defensively.
`getattr(result, "isError", False)` does not raise on 2.x — it reports every
tool-reported error as NOT an error, and the tool's diagnostic text flows on as
evidence. It would never have crashed. It would have shipped.

An AST sweep rather than three pinned names: this SDK renamed the whole family
at once, so a test naming today's three passes on tomorrow's fourth.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# camelCase attributes the mcp SDK renamed. Reading one of these WITHOUT also
# accepting the snake_case spelling is the defect.
RENAMED = {"inputSchema", "isError", "outputSchema"}


def _reads(path):
    """(lineno, attr) for every camelCase SDK attribute read in this file."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return []
    out = []
    for n in ast.walk(tree):
        # obj.inputSchema
        if isinstance(n, ast.Attribute) and n.attr in RENAMED:
            out.append((n.lineno, n.attr))
        # getattr(obj, "isError", ...)
        if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "getattr"
                and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                and n.args[1].value in RENAMED):
            out.append((n.lineno, n.args[1].value))
    return out


def _snake(name: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in name)


def test_no_camelcase_sdk_attribute_is_read_without_its_snake_case_sibling():
    """🔴 A camelCase read is allowed ONLY beside the new spelling. Alone, it
    is either an AttributeError on 2.x or — worse — a silent False."""
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        reads = _reads(path)
        if not reads:
            continue
        text = path.read_text(errors="ignore")
        for lineno, attr in reads:
            if _snake(attr) not in text:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} reads "
                                 f"{attr!r} and never {_snake(attr)!r}")
    assert not offenders, (
        "mcp 2.x renamed these; reading only the old spelling is silent for "
        "isError and fatal for the rest:\n  " + "\n  ".join(offenders)
    )


def test_the_error_accessor_cannot_return_a_silent_false():
    """The whole point: an unrecognised result shape must not read as success
    merely because an attribute name changed."""
    from app.services.mcp_manager import mcp_is_error

    class TwoX:
        is_error = True

    class OneX:
        isError = True

    class Neither:
        pass

    assert mcp_is_error(TwoX()) is True, "2.x spelling not honoured"
    assert mcp_is_error(OneX()) is True, "1.x spelling not honoured"
    assert mcp_is_error(Neither()) is False
    assert mcp_is_error(Neither(), default=True) is True, (
        "the unknown-shape default must be caller-controllable"
    )


def test_the_schema_accessor_reads_either_spelling():
    from app.services.mcp_manager import mcp_input_schema

    class TwoX:
        input_schema = {"type": "object"}

    class OneX:
        inputSchema = {"type": "object"}

    assert mcp_input_schema(TwoX()) == {"type": "object"}
    assert mcp_input_schema(OneX()) == {"type": "object"}
    assert mcp_input_schema(object()) == {}
