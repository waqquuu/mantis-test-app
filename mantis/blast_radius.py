"""Analyze the blast radius of code changes using tree-sitter AST parsing.

Builds a dependency graph across all Python files in a repo, then finds every
function that directly or transitively depends on the changed functions.
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from mantis.diff_parser import ChangedFunction

PY_LANGUAGE = Language(tspython.language())

IGNORED_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "env", "node_modules",
    ".tox", ".mypy_cache", ".pytest_cache", ".eggs", "dist", "build",
}


@dataclass
class BlastRadiusItem:
    file_path: str
    function_name: str
    class_name: str | None
    source: str
    reason: str
    depth: int
    call_site_context: str = ""

    @property
    def qualified_name(self) -> str:
        if self.class_name:
            return f"{self.class_name}.{self.function_name}"
        return self.function_name


@dataclass
class FunctionInfo:
    file_path: str
    function_name: str
    class_name: str | None
    start_line: int
    end_line: int
    source: str
    calls: set[str] = field(default_factory=set)
    imports_from: dict[str, str] = field(default_factory=dict)


def find_blast_radius(
    changed_functions: list[ChangedFunction],
    repo_path: str,
    max_depth: int = 2,
) -> list[BlastRadiusItem]:
    """Find all functions affected by the changed functions, up to max_depth levels."""
    repo = Path(repo_path).resolve()

    all_functions, import_graph, class_locations = _build_project_graph(repo)

    reverse_deps = _build_reverse_dependency_map(
        all_functions, import_graph, repo, class_locations,
    )

    changed_keys = set()
    for cf in changed_functions:
        changed_keys.add(_make_key(cf.file_path, cf.function_name, cf.class_name))

    blast_radius: list[BlastRadiusItem] = []
    visited: set[str] = set()

    # Maps each key to the original changed function that roots its chain
    root_cause: dict[str, str] = {k: k.split(":")[-1] for k in changed_keys}

    def _traverse(target_keys: set[str], depth: int):
        if depth > max_depth:
            return
        next_level_keys: set[str] = set()

        for key in target_keys:
            for caller_key in reverse_deps.get(key, set()):
                if caller_key in visited or caller_key in changed_keys:
                    continue
                visited.add(caller_key)
                func_info = all_functions.get(caller_key)
                if not func_info:
                    continue

                callee_name = func_info_name(all_functions, key)
                origin = root_cause.get(key, callee_name)

                if depth == 1:
                    reason = f"calls {callee_name} which was modified"
                else:
                    reason = f"calls {callee_name} which depends on modified {origin}"

                call_context = _extract_call_site_context(
                    func_info, callee_name, repo
                )

                blast_radius.append(
                    BlastRadiusItem(
                        file_path=func_info.file_path,
                        function_name=func_info.function_name,
                        class_name=func_info.class_name,
                        source=func_info.source,
                        reason=reason,
                        depth=depth,
                        call_site_context=call_context,
                    )
                )
                next_level_keys.add(caller_key)
                root_cause[caller_key] = origin

        _traverse(next_level_keys, depth + 1)

    _traverse(changed_keys, 1)

    return blast_radius


def _extract_call_site_context(
    caller: FunctionInfo, callee_name: str, repo: Path
) -> str:
    """Extract the lines around where the caller invokes the callee, showing
    how the arguments are constructed."""
    source_lines = caller.source.splitlines()
    bare_name = callee_name.split(".")[-1]

    context_snippets: list[str] = []
    for i, line in enumerate(source_lines):
        if bare_name in line and ("(" in line):
            start = max(0, i - 3)
            end = min(len(source_lines), i + 2)
            snippet = "\n".join(source_lines[start:end])
            context_snippets.append(snippet)

    return "\n---\n".join(context_snippets) if context_snippets else ""


def func_info_name(all_functions: dict[str, FunctionInfo], key: str) -> str:
    info = all_functions.get(key)
    if info:
        if info.class_name:
            return f"{info.class_name}.{info.function_name}"
        return info.function_name
    return key.split(":")[-1]


def _make_key(file_path: str, function_name: str, class_name: str | None = None) -> str:
    if class_name:
        return f"{file_path}:{class_name}.{function_name}"
    return f"{file_path}:{function_name}"


CACHE_VERSION = 1


def _get_cache_path(repo: Path) -> Path:
    """Return a per-repo cache path under the user's XDG cache directory."""
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    repo_id = hashlib.md5(str(repo).encode()).hexdigest()
    return cache_home / "mantis" / repo_id / "graph.json"


def _hash_file(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _load_cache(repo: Path) -> dict | None:
    cache_path = _get_cache_path(repo)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if data.get("version") != CACHE_VERSION:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(repo: Path, cache_data: dict) -> None:
    try:
        cache_path = _get_cache_path(repo)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache_data), encoding="utf-8")
    except OSError:
        pass


def _serialize_function(func: FunctionInfo) -> dict:
    return {
        "file_path": func.file_path,
        "function_name": func.function_name,
        "class_name": func.class_name,
        "start_line": func.start_line,
        "end_line": func.end_line,
        "source": func.source,
        "calls": sorted(func.calls),
    }


def _deserialize_function(data: dict) -> FunctionInfo:
    return FunctionInfo(
        file_path=data["file_path"],
        function_name=data["function_name"],
        class_name=data["class_name"],
        start_line=data["start_line"],
        end_line=data["end_line"],
        source=data["source"],
        calls=set(data["calls"]),
    )


def _process_file(
    py_file: Path, repo: Path, cached_files: dict,
) -> tuple[str, list[FunctionInfo], dict[str, str], list[str], dict] | None:
    """Process a single Python file: hash, check cache, parse if needed.

    Returns (rel_path, functions, imports, class_names, cache_entry) or None on error.
    Thread-safe: creates its own Parser instance.
    """
    rel_path = str(py_file.relative_to(repo))

    try:
        content_hash = _hash_file(py_file)
    except OSError:
        return None

    cached_entry = cached_files.get(rel_path)

    if cached_entry and cached_entry.get("hash") == content_hash:
        funcs = [_deserialize_function(fd) for fd in cached_entry["functions"]]
        return (rel_path, funcs, cached_entry["imports"],
                cached_entry.get("classes", []), cached_entry)

    try:
        source = py_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source.encode("utf-8"))
    root = tree.root_node

    functions = _extract_all_functions(root, source, rel_path)
    imports = _extract_imports(root, rel_path, repo)
    class_names = _extract_class_names(root)

    cache_entry = {
        "hash": content_hash,
        "functions": [_serialize_function(f) for f in functions],
        "imports": imports,
        "classes": class_names,
    }
    return (rel_path, functions, imports, class_names, cache_entry)


def _build_project_graph(
    repo: Path,
) -> tuple[dict[str, FunctionInfo], dict[str, dict[str, str]], dict[str, set[str]]]:
    """Parse every Python file and build function info + import maps + class registry.

    Uses a file-content-hash cache in ~/.cache/mantis/ so only modified files
    are re-parsed on subsequent runs.

    Returns:
        all_functions: {key: FunctionInfo}
        import_graph: {file_path: {imported_name: source_module_path}}
        class_locations: {class_name: {file_paths where the class is defined}}
    """
    cache = _load_cache(repo)
    cached_files = cache.get("files", {}) if cache else {}

    all_functions: dict[str, FunctionInfo] = {}
    import_graph: dict[str, dict[str, str]] = {}
    class_locations: dict[str, set[str]] = {}

    python_files = _collect_python_files(repo)
    new_cache_files: dict[str, dict] = {}

    with ThreadPoolExecutor() as pool:
        futures = {
            pool.submit(_process_file, py_file, repo, cached_files): py_file
            for py_file in python_files
        }

        for future in futures:
            result = future.result()
            if result is None:
                continue

            rel_path, funcs, imports, class_names, cache_entry = result

            for func in funcs:
                key = _make_key(func.file_path, func.function_name, func.class_name)
                all_functions[key] = func

            import_graph[rel_path] = imports

            for cls_name in class_names:
                class_locations.setdefault(cls_name, set()).add(rel_path)

            new_cache_files[rel_path] = cache_entry

    _save_cache(repo, {"version": CACHE_VERSION, "files": new_cache_files})

    return all_functions, import_graph, class_locations


def _extract_class_names(root_node) -> list[str]:
    """Extract class names defined in a file's AST."""
    names: list[str] = []

    def _walk(node):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                names.append(name_node.text.decode("utf-8"))
        for child in node.children:
            _walk(child)

    _walk(root_node)
    return names


def _build_reverse_dependency_map(
    all_functions: dict[str, FunctionInfo],
    import_graph: dict[str, dict[str, str]],
    repo: Path,
    class_locations: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """Build a map of {callee_key: set(caller_keys)}.

    For each function, resolve its calls through the import graph to find
    which defined functions it's actually calling.
    """
    reverse_deps: dict[str, set[str]] = {}

    for caller_key, func_info in all_functions.items():
        file_imports = import_graph.get(func_info.file_path, {})

        for call_name in func_info.calls:
            resolved_keys = _resolve_call(
                call_name, func_info.file_path, file_imports,
                all_functions, import_graph, class_locations,
            )
            for callee_key in resolved_keys:
                if callee_key != caller_key:
                    reverse_deps.setdefault(callee_key, set()).add(caller_key)

    return reverse_deps


def _follow_reexport(
    name: str,
    source_file: str,
    all_functions: dict[str, FunctionInfo],
    import_graph: dict[str, dict[str, str]],
    max_hops: int = 5,
) -> str | None:
    """Follow re-export chains (typically through __init__.py files).

    If source_file:name isn't a real definition, check whether source_file
    itself imports `name` from somewhere else, and follow the chain until
    we find the actual definition or exhaust max_hops.
    """
    visited: set[str] = set()
    current_file = source_file

    for _ in range(max_hops):
        key = f"{current_file}:{name}"
        if key in all_functions:
            return key
        if current_file in visited:
            break
        visited.add(current_file)

        file_imports = import_graph.get(current_file, {})
        if name in file_imports:
            current_file = file_imports[name]
        else:
            break

    return None


def _resolve_call(
    call_name: str,
    caller_file: str,
    file_imports: dict[str, str],
    all_functions: dict[str, FunctionInfo],
    import_graph: dict[str, dict[str, str]],
    class_locations: dict[str, set[str]] | None = None,
) -> list[str]:
    """Resolve a function call name to actual function keys in the project."""
    resolved: list[str] = []

    # Direct call to a function in the same file
    same_file_key = f"{caller_file}:{call_name}"
    if same_file_key in all_functions:
        resolved.append(same_file_key)

    # Imported name: from module import func -> func()
    if call_name in file_imports:
        source_file = file_imports[call_name]
        imported_key = f"{source_file}:{call_name}"
        if imported_key in all_functions:
            resolved.append(imported_key)
        else:
            actual = _follow_reexport(
                call_name, source_file, all_functions, import_graph
            )
            if actual and actual not in resolved:
                resolved.append(actual)

    # Attribute call: obj.method() -- check if obj is an imported module or class
    if "." in call_name:
        parts = call_name.split(".", 1)
        obj_name, attr_name = parts[0], parts[1]
        if obj_name in file_imports:
            source_file = file_imports[obj_name]

            # Module-level function: source_file:attr_name
            attr_key = f"{source_file}:{attr_name}"
            if attr_key in all_functions:
                if attr_key not in resolved:
                    resolved.append(attr_key)
            else:
                actual = _follow_reexport(
                    attr_name, source_file, all_functions, import_graph
                )
                if actual and actual not in resolved:
                    resolved.append(actual)

            # Class method: source_file:ClassName.method
            class_method_key = f"{source_file}:{obj_name}.{attr_name}"
            if class_method_key in all_functions:
                if class_method_key not in resolved:
                    resolved.append(class_method_key)
            else:
                target = _follow_class_method(
                    obj_name, attr_name, source_file,
                    all_functions, import_graph,
                )
                if target and target not in resolved:
                    resolved.append(target)

        # Same-file class method: caller_file:ClassName.method
        same_file_method = f"{caller_file}:{call_name}"
        if same_file_method in all_functions and same_file_method not in resolved:
            resolved.append(same_file_method)

        # Fallback: use class_locations registry for unresolved class methods
        if not resolved and class_locations and obj_name in class_locations:
            for cls_file in class_locations[obj_name]:
                candidate = f"{cls_file}:{obj_name}.{attr_name}"
                if candidate in all_functions and candidate not in resolved:
                    resolved.append(candidate)

    return resolved


def _follow_class_method(
    class_name: str,
    method_name: str,
    source_file: str,
    all_functions: dict[str, FunctionInfo],
    import_graph: dict[str, dict[str, str]],
    max_hops: int = 5,
) -> str | None:
    """Follow re-export chains to find where a class method is defined.

    E.g. ``flask/__init__.py`` re-exports ``Flask`` from ``flask.app``,
    so ``Flask.method`` should resolve to ``flask/app.py:Flask.method``.
    """
    visited: set[str] = set()
    current_file = source_file

    for _ in range(max_hops):
        key = f"{current_file}:{class_name}.{method_name}"
        if key in all_functions:
            return key
        if current_file in visited:
            break
        visited.add(current_file)

        file_imports = import_graph.get(current_file, {})
        if class_name in file_imports:
            current_file = file_imports[class_name]
        else:
            break

    return None


def _extract_all_functions(
    root_node,
    source: str,
    file_path: str,
) -> list[FunctionInfo]:
    """Extract all function definitions with their call sites from a file's AST."""
    functions: list[FunctionInfo] = []
    lines = source.splitlines()

    def _walk(node, class_name: str | None = None, class_attr_types: dict[str, str] | None = None):
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            cls_name = name_node.text.decode("utf-8") if name_node else None
            attr_types = _extract_class_attr_types(node) if cls_name else {}
            for child in node.children:
                _walk(child, class_name=cls_name, class_attr_types=attr_types)

        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = name_node.text.decode("utf-8")
                start = node.start_point[0] + 1
                end = node.end_point[0] + 1
                func_source = "\n".join(lines[start - 1 : end])
                calls = _extract_calls(
                    node,
                    class_name=class_name,
                    class_attr_types=class_attr_types,
                )

                functions.append(
                    FunctionInfo(
                        file_path=file_path,
                        function_name=func_name,
                        class_name=class_name,
                        start_line=start,
                        end_line=end,
                        source=func_source,
                        calls=calls,
                    )
                )
            for child in node.children:
                _walk(child, class_name=class_name, class_attr_types=class_attr_types)
        else:
            for child in node.children:
                _walk(child, class_name=class_name, class_attr_types=class_attr_types)

    _walk(root_node)
    return functions


def _extract_class_attr_types(class_node) -> dict[str, str]:
    """Scan a class's ``__init__`` for ``self.attr = Type(...)`` or
    ``self.attr = param`` (where param has a type annotation) to build
    an attribute→type mapping used for resolving ``self.attr.method()`` chains."""
    attr_types: dict[str, str] = {}

    for child in class_node.children:
        if child.type != "function_definition":
            continue
        name_node = child.child_by_field_name("name")
        if not name_node or name_node.text.decode("utf-8") != "__init__":
            continue

        param_types: dict[str, str] = {}
        _extract_param_types(child, param_types)

        body = child.child_by_field_name("body")
        if body:
            _scan_init_body(body, attr_types, param_types)
        break

    return attr_types


def _scan_init_body(
    body_node,
    attr_types: dict[str, str],
    param_types: dict[str, str],
):
    """Scan __init__ body for ``self.attr = ClassName(...)`` and
    ``self.attr = param`` patterns."""
    for node in body_node.children:
        if node.type != "expression_statement":
            continue
        expr = node.children[0] if node.children else None
        if not expr or expr.type != "assignment":
            continue

        left = expr.child_by_field_name("left")
        right = expr.child_by_field_name("right")
        if not left or not right or left.type != "attribute":
            continue

        left_text = left.text.decode("utf-8")
        if not left_text.startswith("self."):
            continue
        attr_name = left_text[5:]
        if "." in attr_name:
            continue

        if right.type == "call":
            func = right.child_by_field_name("function")
            if func and func.type == "identifier":
                type_name = func.text.decode("utf-8")
                if type_name and type_name[0].isupper():
                    attr_types[attr_name] = type_name
        elif right.type == "identifier":
            rhs_name = right.text.decode("utf-8")
            if rhs_name in param_types:
                attr_types[attr_name] = param_types[rhs_name]


def _extract_calls(
    func_node,
    class_name: str | None = None,
    class_attr_types: dict[str, str] | None = None,
) -> set[str]:
    """Extract all function/method call names within a function body.

    Resolves self.method() and cls.method() to ClassName.method when
    class_name is provided.  Tracks variable types from constructor
    calls and parameter annotations to resolve instance.method() calls.
    """
    calls: set[str] = set()
    var_types: dict[str, str] = {}

    _extract_param_types(func_node, var_types)

    def _walk(node):
        if node.type == "assignment":
            _track_constructor(node, var_types)

        if node.type == "call":
            func = node.child_by_field_name("function")
            if func:
                if func.type == "identifier":
                    calls.add(func.text.decode("utf-8"))
                elif func.type == "attribute":
                    raw = func.text.decode("utf-8")
                    resolved = _resolve_attribute_call(
                        raw, class_name, var_types, class_attr_types,
                    )
                    calls.add(resolved)
        for child in node.children:
            _walk(child)

    body = func_node.child_by_field_name("body")
    if body:
        _walk(body)
    return calls


def _resolve_attribute_call(
    raw: str,
    class_name: str | None,
    var_types: dict[str, str],
    class_attr_types: dict[str, str] | None = None,
) -> str:
    """Resolve attribute calls using type information.

    self.method()       → ClassName.method
    self.attr.method()  → AttrType.method  (via __init__ attribute tracking)
    cls.method()        → ClassName.method
    var.method()        → Type.method      (via constructor / annotation tracking)
    """
    parts = raw.split(".", 1)
    if len(parts) != 2:
        return raw

    obj, rest = parts

    if obj == "self" and class_name:
        sub_parts = rest.split(".", 1)
        if len(sub_parts) == 2 and class_attr_types:
            attr_name, method_name = sub_parts
            if attr_name in class_attr_types:
                return f"{class_attr_types[attr_name]}.{method_name}"
        return f"{class_name}.{rest}"

    if obj == "cls" and class_name:
        return f"{class_name}.{rest}"

    if obj in var_types:
        return f"{var_types[obj]}.{rest}"

    return raw


def _extract_param_types(func_node, var_types: dict[str, str]):
    """Extract type annotations from function parameters.

    For ``def foo(self, app: Flask, ctx: AppContext)`` adds
    ``{"app": "Flask", "ctx": "AppContext"}``.
    """
    params = func_node.child_by_field_name("parameters")
    if not params:
        return

    for child in params.children:
        if child.type in ("typed_parameter", "typed_default_parameter"):
            name_node = child.child_by_field_name("name")
            type_node = child.child_by_field_name("type")
            if name_node and type_node:
                param_name = name_node.text.decode("utf-8")
                if param_name in ("self", "cls"):
                    continue
                type_text = type_node.text.decode("utf-8")
                if type_text.isidentifier():
                    var_types[param_name] = type_text


def _track_constructor(assign_node, var_types: dict[str, str]):
    """Track variable types from constructor calls.

    ``app = Flask(__name__)``  →  ``{"app": "Flask"}``
    """
    left = assign_node.child_by_field_name("left")
    right = assign_node.child_by_field_name("right")

    if not left or not right or left.type != "identifier":
        return

    var_name = left.text.decode("utf-8")

    if right.type == "call":
        func = right.child_by_field_name("function")
        if func and func.type == "identifier":
            cls_name = func.text.decode("utf-8")
            if cls_name and cls_name[0].isupper():
                var_types[var_name] = cls_name


def _extract_imports(
    root_node,
    file_path: str,
    repo: Path,
) -> dict[str, str]:
    """Extract import mappings: {imported_name: resolved_file_path}.

    Handles:
    - from module import name
    - from module import name as alias
    - import module
    - from .relative import name
    """
    imports: dict[str, str] = {}

    def _walk(node):
        if node.type == "import_from_statement":
            module_name, prefix_dots = _parse_import_from_module(node)

            resolved_file = _resolve_module_path(
                module_name, prefix_dots, file_path, repo
            )

            imported_names = _parse_imported_names(node)
            for name, alias in imported_names:
                if resolved_file:
                    imports[alias] = resolved_file

        elif node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    module_name = child.text.decode("utf-8")
                    resolved = _resolve_module_path(module_name, "", file_path, repo)
                    if resolved:
                        imports[module_name.split(".")[-1]] = resolved
                elif child.type == "aliased_import":
                    orig = None
                    alias_name = None
                    for sub in child.children:
                        if sub.type == "dotted_name" and orig is None:
                            orig = sub.text.decode("utf-8")
                        elif sub.type == "identifier":
                            alias_name = sub.text.decode("utf-8")
                    if orig:
                        resolved = _resolve_module_path(orig, "", file_path, repo)
                        if resolved:
                            imports[alias_name or orig.split(".")[-1]] = resolved

        if node.type not in ("import_from_statement", "import_statement"):
            for child in node.children:
                _walk(child)

    _walk(root_node)
    return imports


def _parse_import_from_module(node) -> tuple[str, str]:
    """Extract the module name and relative dot prefix from an import_from_statement.

    Handles both absolute (from fastapi.utils import X) and relative
    (from .shared import X) imports, including the `relative_import` →
    `import_prefix` AST structure used by tree-sitter-python.
    """
    module_name = ""
    prefix_dots = ""

    for child in node.children:
        if child.type == "relative_import":
            for sub in child.children:
                if sub.type == "import_prefix":
                    prefix_dots = sub.text.decode("utf-8")
                elif sub.type == "dotted_name":
                    module_name = sub.text.decode("utf-8")
        elif child.type == "dotted_name" and not module_name:
            # For absolute imports the module is a direct dotted_name child,
            # but only if we haven't already found it inside relative_import.
            # Avoid picking up the imported *names* (they come after "import").
            module_field = node.child_by_field_name("module_name")
            if module_field and module_field.id == child.id:
                module_name = child.text.decode("utf-8")

    return module_name, prefix_dots


def _parse_imported_names(node) -> list[tuple[str, str]]:
    """Extract (original_name, alias) pairs from an import_from_statement."""
    names: list[tuple[str, str]] = []
    module_field = node.child_by_field_name("module_name")

    for child in node.children:
        if child.type == "dotted_name":
            # Skip the module name node itself
            if module_field and module_field.id == child.id:
                continue
            # Also skip dotted_names inside relative_import
            if child.parent and child.parent.type == "relative_import":
                continue
            name = child.text.decode("utf-8")
            names.append((name, name))
        elif child.type == "aliased_import":
            orig = None
            alias = None
            for sub in child.children:
                if sub.type == "dotted_name" and orig is None:
                    orig = sub.text.decode("utf-8")
                elif sub.type == "identifier":
                    alias = sub.text.decode("utf-8")
            if orig:
                names.append((orig, alias or orig))

    return names


def _resolve_module_path(
    module_name: str,
    prefix_dots: str,
    current_file: str,
    repo: Path,
) -> str | None:
    """Convert a Python module path to a file path relative to the repo root."""
    if not module_name and not prefix_dots:
        return None

    if prefix_dots:
        levels_up = len(prefix_dots) - 1
        current_dir = Path(current_file).parent
        for _ in range(levels_up):
            current_dir = current_dir.parent
        if module_name:
            rel = current_dir / module_name.replace(".", "/")
        else:
            rel = current_dir
    else:
        rel = Path(module_name.replace(".", "/"))

    # Check module.py
    candidate = repo / f"{rel}.py"
    if candidate.exists():
        return str(rel.with_suffix(".py"))

    # Check module/__init__.py (package)
    candidate = repo / rel / "__init__.py"
    if candidate.exists():
        return str(rel / "__init__.py")

    return None


def _collect_python_files(repo: Path) -> list[Path]:
    """Collect all .py files in the repo, skipping ignored directories."""
    python_files: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                python_files.append(Path(root) / f)
    return python_files
