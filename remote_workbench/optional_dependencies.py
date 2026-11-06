"""Explain how to install an explicitly selected optional feature."""
from importlib.util import find_spec


def require_extra(extra, *modules):
    missing = [name for name in modules if find_spec(name) is None]
    if missing:
        raise SystemExit(
            f"Missing optional dependencies: {', '.join(missing)}. "
            f"Install remote-workbench[{extra}], or in a source checkout run "
            f"'uv sync --frozen --extra {extra}'."
        )
