"""
etna/utils/registry.py — @tool decorator and tool registry

The @tool decorator:
  - Stamps func._kit with the module stem (e.g. "nfty_kit")
  - Registers the function into the global TOOLS dict by its function name
  - Type hints are scraped to build JSON Schema on demand
"""

import inspect

# Global tool registry: { function_name: callable }
TOOLS: dict = {}


def tool(func):
    """
    Decorator to register a function as a callable Etna tool.
    func.__module__ is e.g. "kits.nfty_kit" — take the last segment as the kit stem.
    """
    func._kit = func.__module__.split(".")[-1]
    TOOLS[func.__name__] = func
    return func


def get_tools() -> dict:
    """Return the full tool registry."""
    return TOOLS


def get_tools_for_kit(kit_stem: str) -> dict:
    """Return only tools belonging to the named kit stem."""
    return {
        name: func for name, func in TOOLS.items()
        if getattr(func, "_kit", None) == kit_stem
    }


def extract_parameters(func) -> dict:
    """
    Turn Python type hints into JSON Schema.
    Handles str, int, float, bool; everything else treated as string.
    Parameters without defaults are required.
    """
    sig = inspect.signature(func)
    properties = {}
    required = []

    for name, param in sig.parameters.items():
        annotation = param.annotation

        if annotation == str:
            properties[name] = {"type": "string"}
        elif annotation == int:
            properties[name] = {"type": "integer"}
        elif annotation == float:
            properties[name] = {"type": "number"}
        elif annotation == bool:
            properties[name] = {"type": "boolean"}
        else:
            properties[name] = {"type": "string"}

        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def build_tool_schema(name: str, func) -> dict:
    """Return the full schema dict for a single tool."""
    return {
        "name": name,
        "description": func.__doc__ or "",
        "parameters": extract_parameters(func),
    }
