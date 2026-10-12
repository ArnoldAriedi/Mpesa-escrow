import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _squash(text):
    """Source-text checks must not depend on formatting."""
    return re.sub(r"\s+", "", text)


def _source(name):
    return (ROOT / name).read_text()


def test_delivery_confirmation_uses_pessimistic_row_lock():
    src = _source("escrow_main.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "confirm_delivery")
    fn_src = ast.get_source_segment(src, fn)
    assert ".with_for_update()" in fn_src
    assert "EscrowTransaction.qr_token==p.qr_token" in _squash(fn_src)
    assert "EscrowTransaction.status==EscrowStatus.ESCROW_HELD" in _squash(fn_src)


def test_callback_authentication_is_header_only():
    src = _source("escrow_main.py")
    assert 'alias="X-Mpesa-Secret"' in src
    assert "request.query_params" not in src
    assert "callback_token" not in src
    assert '"/mpesa/{callback_token}/' not in src
    assert _squash(src).count("_require_callback_secret(x_mpesa_secret)") >= 9


def test_callback_urls_do_not_embed_callback_secret():
    src = _source("escrow_core.py")
    start = src.index("def callback_url")
    end = src.index("\ndef ", start + 5)
    fn = src[start:end]
    tree = ast.parse(fn)
    func_node = tree.body[0]
    body = func_node.body
    # Drop the leading docstring before checking: it's allowed to *mention*
    # the secret's name in prose (explaining why the URL doesn't carry it);
    # what actually matters is that the code itself never uses the secret as
    # a value.
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    code_only = ast.unparse(ast.Module(body=body, type_ignores=[]))
    assert "MPESA_CALLBACK_SECRET" not in code_only
    assert 'return f"{base}/callbacks/{endpoint}"' in fn


def test_verify_schema_strips_async_drivers():
    src = _source("scripts/verify_schema.py")
    assert '.replace("+asyncpg", "")' in src
    assert '.replace("+aiosqlite", "")' in src


def test_expiry_sleep_is_inside_try_boundary():
    src = _source("escrow_main.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "expiry_loop")
    # The try/except sits one level inside the worker's `while True:` loop,
    # not as a direct statement of the function body — walk the whole
    # function rather than only its immediate children to find it.
    boundary = next(n for n in ast.walk(fn) if isinstance(n, ast.Try))
    sleeps = [n for n in ast.walk(boundary)
              if isinstance(n, ast.Await)
              and isinstance(n.value, ast.Call)
              and isinstance(n.value.func, ast.Attribute)
              and n.value.func.attr == "sleep"]
    assert sleeps
    assert any(isinstance(h.type, ast.Attribute) and h.type.attr == "CancelledError"
               for h in boundary.handlers)
