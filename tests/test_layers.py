"""What runs where: the agent computer's code never needs your machine's (`core.local`, the agents' `local`), your
machine's commands need none of the agent computer's (`core.agent`, the agents' `agent`), what both sides run needs
neither, and core needs no agent. Every module of `src/` is found by walking its packages and put on one side by its
name, so a new module is checked as soon as it is there; one that fits no side fails until it is given one."""

import ast
import pkgutil
import sys
from fnmatch import fnmatch
from functools import cache
from pathlib import Path

import pytest

SRC = Path(__file__).parents[1] / "src"
PACKAGES = ("core", "m2p", "acc")

# The sides, the first that matches a module's name wins.
SIDES = {
    # Where both sides meet: the `agent` CLI adds each agent's hidden `remote` commands to its own.
    "entry": ("core.__main__", "core.local.cli"),
    # Runs on the MongoDB computer, which has only Python, bson and pymongo.
    "mongo": ("m2p.usecase.seed",),
    "local": ("core.local", "core.local.*", "*.local", "m2p.follow", "m2p.results"),
    "agent": ("core.agent", "core.agent.*", "*.agent", "*.steps", "acc.stack", "acc.cleanup", "acc.pr"),
    # `m2p.usecase` itself is the `[m2p.seed]` settings, part of m2p's config.
    "both": (
        *PACKAGES, "core.config", "core.computer", "core.activity", "core.shell", "core.doomed", "core.nevia",
        "core.results", "*.run", "*.computer", "*.config", "m2p.usecase",
    ),
}  # fmt: skip
MONGO_DEPS = {"bson", "pymongo"}


def _modules() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for package in PACKAGES:
        found[package] = SRC / package / "__init__.py"
        for m in pkgutil.walk_packages([str(SRC / package)], prefix=f"{package}."):
            path = SRC.joinpath(*m.name.split("."))
            found[m.name] = path / "__init__.py" if m.ispkg else path.with_suffix(".py")
    return found


MODULES = _modules()


def _side(module: str) -> str | None:
    return next((side for side, names in SIDES.items() if any(fnmatch(module, n) for n in names)), None)


@cache
def _direct(module: str) -> frozenset[str]:
    """The modules `module` imports, anywhere in it, by its source: the project's ones and the top-level others."""
    path = MODULES[module]
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(
                    [*package.split(".")[: len(package.split(".")) - node.level + 1], *filter(None, [base])]
                )
            names |= {base, *(f"{base}.{a.name}" for a in node.names)}
    out: set[str] = set()
    for name in names:
        parts = name.split(".")
        if parts[0] not in PACKAGES:
            out.add(parts[0])
            continue
        # A module and the packages it is in; `from m import Name` names no module.
        out |= {p for i in range(1, len(parts) + 1) if (p := ".".join(parts[:i])) in MODULES}
    return frozenset(out - {module})


@cache
def _imported(module: str) -> frozenset[str]:
    """Of what importing `module` imports, the project's modules."""
    seen: set[str] = set()
    todo = [module]
    while todo:
        for m in _direct(todo.pop()) & MODULES.keys():
            if m not in seen:
                seen.add(m)
                todo.append(m)
    return frozenset(seen - {module})


def _of(*sides: str) -> list[str]:
    return sorted(m for m in MODULES if _side(m) in sides)


def test_every_module_is_on_a_side() -> None:
    assert sorted(MODULES.values()) == sorted(p for pkg in PACKAGES for p in (SRC / pkg).rglob("*.py"))  # all found
    assert [m for m in MODULES if _side(m) is None] == []


@pytest.mark.parametrize("module", _of("both"))
def test_what_both_sides_run_imports_only_what_both_sides_run(module: str) -> None:
    assert sorted(m for m in _imported(module) if _side(m) != "both") == []


@pytest.mark.parametrize("module", _of("agent"))
def test_the_agent_side_imports_nothing_of_your_machine(module: str) -> None:
    assert sorted(m for m in _imported(module) if _side(m) == "local") == []


@pytest.mark.parametrize("module", _of("local"))
def test_your_machine_s_commands_import_nothing_of_the_agent_computer(module: str) -> None:
    assert sorted(m for m in _imported(module) if _side(m) == "agent") == []


@pytest.mark.parametrize("module", sorted(m for m in MODULES if m.startswith("core") and _side(m) != "entry"))
def test_core_imports_no_agent(module: str) -> None:
    """`core.agent` included: what both agents run on the agent computer knows neither."""
    assert sorted(m for m in _imported(module) if not m.startswith("core")) == []


def test_the_seed_needs_only_python_bson_and_pymongo() -> None:
    [seed] = _of("mongo")
    assert sorted(_direct(seed) - set(sys.stdlib_module_names) - MONGO_DEPS) == []
