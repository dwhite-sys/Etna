"""
etna/kit_linter.py — Pre-install kit validation

Runs before a kit is copied into ~/.etna_server/kits/.
Hard errors block install. Warnings let install proceed but are flagged.

Checks:
  [ERROR]   Syntax error in kit file
  [ERROR]   Missing required metadata fields (kit_name, kit_description, requirements, config)
  [ERROR]   requirements is not a bare list literal
  [ERROR]   config is not a bare dict literal
  [ERROR]   No @tool functions found
  [ERROR]   @tool function missing type hints on one or more parameters
  [WARNING] @tool function missing docstring
  [WARNING] @tool function has no return type annotation
"""

import ast
from pathlib import Path

from etna.console import *


class LintError(Exception):
    pass


def lint_kit(kit_path: Path) -> bool:
    """
    Lint a kit file before install.
    Prints results with colors.
    Returns True if the kit passes (errors=0), False if it should be blocked.
    Warnings don't block install.
    """
    print(f"{PREFIX}{grey}Linting {white}'{light_grey}{kit_path.name}{white}'{grey}...{white}")

    # ── Step 1: Syntax check ──────────────────────────────────────────────────
    try:
        source = kit_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  {red}✘ Syntax error on line {e.lineno}: {light_grey}{e.msg}{white}")
        return False
    except Exception as e:
        print(f"  {red}✘ Could not read kit file: {light_grey}{e}{white}")
        return False

    errors = 0
    warnings = 0

    # ── Step 2: Required metadata fields ─────────────────────────────────────
    found_fields = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            found_fields[target.id] = node.value

    required = ["kit_name", "kit_description", "requirements", "config"]
    for field in required:
        if field not in found_fields:
            print(f"  {red}✘ Missing required field: {light_grey}{field}{white}")
            errors += 1

    # ── Step 3: requirements must be a bare list literal ─────────────────────
    if "requirements" in found_fields:
        if not isinstance(found_fields["requirements"], ast.List):
            print(f"  {red}✘ {light_grey}requirements{white} {red}must be a bare list literal, not a variable or expression{white}")
            errors += 1
        else:
            for elt in found_fields["requirements"].elts:
                if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                    print(f"  {red}✘ {light_grey}requirements{white} {red}list must contain only string literals{white}")
                    errors += 1
                    break

    # ── Step 4: config must be a bare dict literal ────────────────────────────
    if "config" in found_fields:
        if not isinstance(found_fields["config"], ast.Dict):
            print(f"  {red}✘ {light_grey}config{white} {red}must be a bare dict literal, not a variable or expression{white}")
            errors += 1
        else:
            for k, v in zip(found_fields["config"].keys, found_fields["config"].values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    print(f"  {red}✘ {light_grey}config{white} {red}keys must be string literals{white}")
                    errors += 1
                    break
                if not isinstance(v, ast.Constant):
                    print(f"  {red}✘ {light_grey}config{white} {red}values must be JSON scalar literals (str, int, float, bool){white}")
                    errors += 1
                    break

    # ── Step 5: Find @tool functions ──────────────────────────────────────────
    tool_functions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Name) and decorator.id == "tool") or \
               (isinstance(decorator, ast.Attribute) and decorator.attr == "tool"):
                tool_functions.append(node)
                break

    if not tool_functions:
        print(f"  {red}✘ No {light_grey}@tool{white} {red}functions found — kit would expose nothing{white}")
        errors += 1

    # ── Step 6: Per-tool checks ───────────────────────────────────────────────
    for func in tool_functions:
        name = func.name

        # Type hints on all parameters
        missing_hints = []
        for arg in func.args.args:
            if arg.annotation is None:
                missing_hints.append(arg.arg)
        if missing_hints:
            print(f"  {red}✘ {light_grey}{name}{white}{red}: missing type hints on: {light_grey}{', '.join(missing_hints)}{white}")
            print(f"    {grey}(Tools with un-hinted parameters are silently dropped from the schema){white}")
            errors += 1

        # Docstring
        if not (func.body and isinstance(func.body[0], ast.Expr) and
                isinstance(func.body[0].value, ast.Constant) and
                isinstance(func.body[0].value.value, str)):
            print(f"  {yellow}⚠ {light_grey}{name}{white}{yellow}: missing docstring — the model won't know when to use this tool{white}")
            warnings += 1

        # Return annotation (soft warning)
        if func.returns is None:
            print(f"  {yellow}⚠ {light_grey}{name}{white}{yellow}: no return type annotation{white}")
            warnings += 1

    # ── Result ────────────────────────────────────────────────────────────────
    if errors == 0 and warnings == 0:
        print(f"  {green}✔ All checks passed{white}")
    elif errors == 0:
        print(f"  {yellow}✔ Passed with {warnings} warning{'s' if warnings != 1 else ''}{white}")
    else:
        print(f"  {red}✘ {errors} error{'s' if errors != 1 else ''}{white}"
              + (f"{red}, {warnings} warning{'s' if warnings != 1 else ''}{white}" if warnings else "")
              + f" {red}— install blocked{white}")

    return errors == 0
