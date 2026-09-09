#!/usr/bin/env python3
"""Audit plugin exports and cross-plugin imports with Python AST."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "XBotv2"
ALLOWED_DECLARATIONS = {"contracts", "events", "protocol"}
SHARED_ROOTS = {"core", "protocol"}


@dataclass(frozen=True)
class ImportRecord:
    source: str
    module: str
    symbols: tuple[str, ...]
    classification: str


@dataclass(frozen=True)
class ProtocolRecord:
    owner: str
    module: str
    name: str
    required: tuple[str, ...]
    implementers: tuple[str, ...]


@dataclass(frozen=True)
class ExportViolation:
    package: str
    symbol: str
    module: str


@dataclass(frozen=True)
class DeclarationViolation:
    source: str
    module: str
    symbols: tuple[str, ...]


def module_root(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != "XBotv2":
        return None
    return parts[1]


def package_exports(package: Path) -> tuple[str, ...]:
    init = package / "__init__.py"
    if not init.is_file():
        return ()
    tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        ):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return tuple(
                item.value
                for item in node.value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return ()


def package_export_violations(package: Path) -> list[ExportViolation]:
    init = package / "__init__.py"
    if not init.is_file():
        return []
    tree = ast.parse(init.read_text(encoding="utf-8"), filename=str(init))
    exports = set(package_exports(package))
    dynamic_export = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__getattr__"
        ),
        None,
    )
    violations = (
        [ExportViolation(package.name, "__getattr__", "<dynamic package export>")]
        if dynamic_export is not None
        else []
    )
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = node.module
    for symbol in sorted(exports):
        module = imports.get(symbol)
        if module is None:
            violations.append(ExportViolation(
                package.name,
                symbol,
                "<unresolved package export>",
            ))
            continue
        leaf = module.rsplit(".", 1)[-1]
        if leaf not in ALLOWED_DECLARATIONS and not module.startswith("XBotv2.core"):
            violations.append(ExportViolation(package.name, symbol, module))
    return violations


def declaration_import_violations(package: Path) -> list[DeclarationViolation]:
    """Reject public declarations that depend on same-package implementations."""
    violations: list[DeclarationViolation] = []
    prefix = f"XBotv2.{package.name}."
    for leaf in sorted(ALLOWED_DECLARATIONS):
        path = package / f"{leaf}.py"
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(prefix):
                continue
            target_leaf = node.module.rsplit(".", 1)[-1]
            if target_leaf in ALLOWED_DECLARATIONS:
                continue
            violations.append(DeclarationViolation(
                source=str(path.relative_to(ROOT)),
                module=node.module,
                symbols=tuple(sorted(alias.name for alias in node.names)),
            ))
    return violations


def classify(owner: str, module: str) -> str:
    imported = module_root(module)
    if imported is None or imported == owner:
        return "local/non-plugin"
    leaf = module.rsplit(".", 1)[-1]
    if imported in SHARED_ROOTS:
        return "allowed shared contract"
    if owner == "acp_plugin" and module == "XBotv2.application.acp":
        return "host composition exception"
    if leaf in ALLOWED_DECLARATIONS:
        return "allowed plugin contract"
    if module == f"XBotv2.{imported}":
        return "validated package-root API (__all__)"
    return "IMPLEMENTATION DEPENDENCY"


def imports_for(package: Path, owner: str) -> list[ImportRecord]:
    records: dict[tuple[str, str, str], set[str]] = {}
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports = [(alias.name, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports = [(node.module, alias.name) for alias in node.names]
            else:
                continue
            for module, symbol in imports:
                if module_root(module) not in {None, owner}:
                    key = (str(path.relative_to(ROOT)), module, classify(owner, module))
                    records.setdefault(key, set()).add(symbol)
    return [
        ImportRecord(source, module, tuple(sorted(symbols)), classification)
        for (source, module, classification), symbols in sorted(records.items())
    ]


def _protocol_methods(node: ast.ClassDef) -> set[str]:
    return {
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def protocol_records(package: Path, owner: str) -> list[ProtocolRecord]:
    protocols: list[tuple[str, str, set[str]]] = []
    classes: list[tuple[str, str, set[str]]] = []
    contract_path = package / "contracts.py"
    if contract_path.is_file():
        tree = ast.parse(
            contract_path.read_text(encoding="utf-8"),
            filename=str(contract_path),
        )
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = _protocol_methods(node)
            bases = {
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            }
            if "Protocol" in bases:
                protocols.append((str(contract_path.relative_to(ROOT)), node.name, methods))
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name == "contracts.py" or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            methods = _protocol_methods(node)
            qualified = f"{path.relative_to(ROOT)}:{node.name}"
            if methods:
                classes.append((qualified, node.name, methods))
    result: list[ProtocolRecord] = []
    for module, name, required in protocols:
        candidates = tuple(
            qualified
            for qualified, _class_name, methods in classes
            if required <= methods
        )
        result.append(ProtocolRecord(owner, module, name, tuple(sorted(required)), candidates))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=("markdown", "tsv"), default="markdown")
    args = parser.parse_args()
    packages = sorted(path.parent for path in PACKAGE.glob("*/plugin.py"))
    violations = 0
    if args.format == "markdown":
        print("# Plugin Contract AST Audit")
        print()
        print("Generated from Python AST; imports are reported at symbol level.")
        print()
    for package in packages:
        owner = package.name
        exports = package_exports(package)
        imports = imports_for(package, owner)
        implementation_imports = [
            record for record in imports
            if record.classification == "IMPLEMENTATION DEPENDENCY"
        ]
        export_violations = package_export_violations(package)
        declaration_violations = declaration_import_violations(package)
        violations += (
            len(implementation_imports)
            + len(export_violations)
            + len(declaration_violations)
        )
        if args.format == "markdown":
            print(f"## {owner}")
            print()
            protocols = protocol_records(package, owner)
            if protocols:
                print("**Protocol implementation coverage:**")
                for record in protocols:
                    candidates = ", ".join(f"`{item}`" for item in record.implementers)
                    print(
                        f"- `{record.module}:{record.name}`; required methods: "
                        f"{', '.join(f'`{item}`' for item in record.required) or '(none)'}; "
                        f"structural candidates: {candidates or '(none)'}"
                    )
                print()
            print("**Package exports:** " + (", ".join(f"`{x}`" for x in exports) or "(none)"))
            if export_violations:
                print("**IMPLEMENTATION RE-EXPORTS:**")
                for violation in export_violations:
                    print(f"- `{violation.symbol}` from `{violation.module}`")
            if declaration_violations:
                print("**DECLARATION IMPLEMENTATION DEPENDENCIES:**")
                for violation in declaration_violations:
                    print(
                        f"- `{violation.source}` imports "
                        f"`{', '.join(violation.symbols)}` from `{violation.module}`"
                    )
            print()
            print("| Source | Imported module | Symbols | Classification |")
            print("|---|---|---|---|")
            if imports:
                for record in imports:
                    print(
                        f"| `{record.source}` | `{record.module}` | "
                        f"{', '.join(f'`{x}`' for x in record.symbols)} | "
                        f"{record.classification} |"
                    )
            else:
                print("| - | - | - | - |")
            print()
        else:
            for record in imports:
                print(
                    "\t".join((owner, ",".join(exports), record.source,
                               record.module, ",".join(record.symbols),
                               record.classification))
                )
            for violation in export_violations:
                print(
                    "\t".join((owner, ",".join(exports), "__init__.py",
                               violation.module, violation.symbol,
                               "IMPLEMENTATION RE-EXPORT"))
                )
            for violation in declaration_violations:
                print(
                    "\t".join((owner, ",".join(exports), violation.source,
                               violation.module, ",".join(violation.symbols),
                               "DECLARATION IMPLEMENTATION DEPENDENCY"))
                )
    if violations:
        print(f"\ncontract audit: {violations} violation(s)")
        return 1
    print("contract audit: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
