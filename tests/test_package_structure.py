import ast
from pathlib import Path


HOOK_DIR = Path(__file__).resolve().parents[1]

EXPECTED_ROOT_FILES = {"handler.py"}
EXPECTED_LAYER_DIRS = {"domain", "features", "hermes_adapter", "infrastructure"}

ALLOWED_PACKAGE_IMPORTS = {
    "domain": set(),
    "domain.config": {"domain.constants"},
    "domain.constants": set(),
    "domain.invocation": set(),
    "domain.state": set(),
    "features": set(),
    "features.anthropic_client": {
        "domain.config",
        "domain.constants",
        "domain.invocation",
        "infrastructure.claude_cli",
        "infrastructure.content",
        "infrastructure.live_session",
        "infrastructure.mcp_bridge",
        "infrastructure.streaming",
    },
    "hermes_adapter": set(),
    "hermes_adapter.exports": {
        "domain",
        "domain.config",
        "domain.constants",
        "domain.invocation",
        "domain.state",
        "features",
        "features.anthropic_client",
        "hermes_adapter",
        "hermes_adapter.auxiliary",
        "hermes_adapter.hook",
        "hermes_adapter.prompt_guidance",
        "hermes_adapter.runtime_selection",
        "infrastructure",
        "infrastructure.claude_cli",
        "infrastructure.content",
        "infrastructure.live_session",
        "infrastructure.mcp_bridge",
        "infrastructure.streaming",
    },
    "hermes_adapter.auxiliary": {
        "domain.config",
        "domain.constants",
        "features.anthropic_client",
        "hermes_adapter.runtime_selection",
    },
    "hermes_adapter.hook": {
        "hermes_adapter.auxiliary",
        "domain.config",
        "domain.constants",
        "domain.state",
        "features.anthropic_client",
        "hermes_adapter.prompt_guidance",
        "hermes_adapter.runtime_selection",
        "infrastructure.claude_cli",
    },
    "hermes_adapter.prompt_guidance": set(),
    "hermes_adapter.runtime_selection": {
        "domain.constants",
        "domain.state",
        "features.anthropic_client",
    },
    "infrastructure": set(),
    "infrastructure.claude_cli": {
        "domain.config",
        "domain.constants",
        "domain.state",
    },
    "infrastructure.content": {
        "domain.constants",
        "infrastructure.claude_cli",
    },
    "infrastructure.live_session": {
        "domain.constants",
        "domain.invocation",
        "infrastructure.claude_cli",
    },
    "infrastructure.mcp_bridge": {
        "domain.config",
        "domain.constants",
        "infrastructure.claude_cli",
    },
    "infrastructure.streaming": {
        "domain.constants",
        "domain.invocation",
        "infrastructure.claude_cli",
        "infrastructure.live_session",
    },
}

FORBIDDEN_TOP_LEVEL_CORE_IMPORTS = {
    "agent",
    "gateway",
    "hermes",
    "hermes_cli",
    "run_agent",
    "tools",
}


def _source_paths():
    for layer in EXPECTED_LAYER_DIRS:
        yield from (HOOK_DIR / layer).rglob("*.py")


def _module_name(path):
    relative = path.relative_to(HOOK_DIR).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts)


def _known_package_modules():
    return {_module_name(path) for path in _source_paths()}


def _absolute_package_imports(tree, known_modules):
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                for index in range(len(parts), 0, -1):
                    candidate = ".".join(parts[:index])
                    if candidate in known_modules:
                        imports.add(candidate)
                        break
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_parts = node.module.split(".")
            base_module = ".".join(module_parts)
            if base_module in known_modules:
                imports.add(base_module)
            for alias in node.names:
                candidate = ".".join(module_parts + alias.name.split("."))
                if candidate in known_modules:
                    imports.add(candidate)
    return imports


def _relative_import_target(module_name, node, alias_name, known_modules):
    current_package = tuple(module_name.split(".")[:-1])
    if module_name in EXPECTED_LAYER_DIRS:
        current_package = tuple(module_name.split("."))
    up_count = max(node.level - 1, 0)
    if up_count:
        current_package = current_package[:-up_count]
    module_parts = tuple(node.module.split(".")) if node.module else ()
    base_parts = current_package + module_parts
    alias_parts = base_parts + tuple(alias_name.split("."))
    alias_module = ".".join(alias_parts)
    base_module = ".".join(base_parts)
    if alias_module in known_modules:
        return alias_module
    return base_module


def _relative_package_imports(module_name, tree, known_modules):
    imports = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level == 0:
            continue
        for alias in node.names:
            target = _relative_import_target(module_name, node, alias.name, known_modules)
            if target in known_modules:
                imports.add(target)
    return imports


def _top_level_core_imports(tree):
    imports = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports & FORBIDDEN_TOP_LEVEL_CORE_IMPORTS


def test_hook_root_uses_memory_lancedb_style_layers_without_package_wrapper():
    source_dirs = {
        path.name
        for path in HOOK_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(".") and path.name not in {"__pycache__", "tests"}
    }
    top_level_py_files = {path.name for path in HOOK_DIR.glob("*.py")}
    assert source_dirs == EXPECTED_LAYER_DIRS
    assert top_level_py_files == EXPECTED_ROOT_FILES
    assert not (HOOK_DIR / "hermes_claude_cli_mode").exists()


def test_package_imports_follow_declared_layer_dependency_graph():
    known_modules = _known_package_modules()
    assert known_modules == set(ALLOWED_PACKAGE_IMPORTS)

    for path in _source_paths():
        module_name = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        unexpected = (
            _absolute_package_imports(tree, known_modules)
            | _relative_package_imports(module_name, tree, known_modules)
        ) - ALLOWED_PACKAGE_IMPORTS[module_name]
        assert not unexpected, f"{module_name} imports unexpected module(s): {sorted(unexpected)}"


def test_source_modules_do_not_import_hermes_core_at_module_load_time():
    for path in _source_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forbidden = _top_level_core_imports(tree)
        assert not forbidden, f"{_module_name(path)} imports Hermes core module(s) at import time: {sorted(forbidden)}"


def test_handler_entrypoint_does_not_own_runtime_implementation():
    tree = ast.parse((HOOK_DIR / "handler.py").read_text(encoding="utf-8"))
    implementation_nodes = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
    )
    assert not any(isinstance(node, implementation_nodes) for node in ast.walk(tree))
