"""Python code parser using AST module."""

import ast
from typing import List, Optional, Dict, Set
from pathlib import Path

from src.datamodels import Node, Edge, UnresolvedCall, ParseResult


class PythonParser:
    """Parser for Python code using AST.

    Throughout this module the local name `ast_node` always refers to a node
    of the Python `ast` module. The unqualified name `Node` (with capital N)
    is reserved for our graph node dataclass from `src.datamodels`.
    """

    def __init__(self):
        self.nodes: List[Node] = []
        self.edges: List[Edge] = []
        self.unresolved_calls: List[UnresolvedCall] = []
        self.current_file: str = ""
        self.current_class_stack: List[str] = []  # Track nested classes
        self.defined_names: Dict[str, str] = {}  # name -> node_id mapping
        self.imports: Dict[str, str] = {}  # imported name -> module

    def parse_file(self, file_path: str) -> ParseResult:
        """Parse a single Python file."""
        self.current_file = file_path
        self.nodes = []
        self.edges = []
        self.current_class_stack = []
        self.defined_names = {}
        self.imports = {}

        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()

        return self.parse_string(source, file_path)

    def parse_string(self, source: str, file_path: str = "<string>") -> ParseResult:
        """Parse Python code from a string."""
        self.current_file = file_path
        self.nodes = []
        self.edges = []
        self.unresolved_calls = []
        self.current_class_stack = []
        self.defined_names = {}
        self.imports = {}

        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            print(f"Syntax error in {file_path}: {e}")
            return ParseResult()

        # Create File node - use full path for unique ID
        file_node = Node(
            id=self._make_id("File", file_path, file_path),
            node_type="File",
            name=Path(file_path).name,
            file_path=file_path
        )
        self.nodes.append(file_node)

        # First pass: collect all definitions and imports
        self._collect_definitions(tree)

        # Second pass: extract structure and calls
        self._visit_module(tree, file_node.id)

        return ParseResult(nodes=self.nodes, edges=self.edges, unresolved_calls=self.unresolved_calls)

    def _collect_definitions(self, tree: ast.AST) -> None:
        """First pass: collect all class and function definitions."""
        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.ClassDef):
                node_id = self._make_id("Class", ast_node.name, self.current_file)
                self.defined_names[ast_node.name] = node_id
            elif isinstance(ast_node, ast.FunctionDef) or isinstance(ast_node, ast.AsyncFunctionDef):
                # Only top-level functions (not methods)
                node_id = self._make_id("Function", ast_node.name, self.current_file)
                self.defined_names[ast_node.name] = node_id
            elif isinstance(ast_node, ast.Import):
                for alias in ast_node.names:
                    name = alias.asname if alias.asname else alias.name
                    self.imports[name] = alias.name
            elif isinstance(ast_node, ast.ImportFrom):
                module = ast_node.module or ""
                for alias in ast_node.names:
                    name = alias.asname if alias.asname else alias.name
                    self.imports[name] = f"{module}.{alias.name}"

    def _visit_module(self, tree: ast.Module, file_id: str) -> None:
        """Visit module-level nodes."""
        for ast_node in tree.body:
            if isinstance(ast_node, ast.ClassDef):
                self._visit_class(ast_node, file_id)
            elif isinstance(ast_node, ast.FunctionDef) or isinstance(ast_node, ast.AsyncFunctionDef):
                self._visit_function(ast_node, file_id, is_method=False)
            # Extract module-level calls (e.g., class decorators, global assignments)
            self._extract_calls_from_ast_node(ast_node, file_id)

    def _visit_class(self, ast_node: ast.ClassDef, parent_id: str) -> None:
        """Visit a class definition."""
        class_id = self._make_id("Class", ast_node.name, self.current_file)

        class_node = Node(
            id=class_id,
            node_type="Class",
            name=ast_node.name,
            file_path=self.current_file,
            line_start=ast_node.lineno,
            line_end=ast_node.end_lineno,
            is_callable=False
        )
        self.nodes.append(class_node)

        # Edge: parent contains this class
        self.edges.append(Edge(
            source_id=parent_id,
            target_id=class_id,
            edge_type="contains"
        ))

        # Handle inheritance
        for base in ast_node.bases:
            base_name = self._get_name_from_ast_node(base)
            if base_name:
                base_id = self.defined_names.get(base_name)
                if base_id:
                    # Bidirectional inheritance - add edge in both directions
                    self.edges.append(Edge(
                        source_id=class_id,
                        target_id=base_id,
                        edge_type="inherits"
                    ))
                    self.edges.append(Edge(
                        source_id=base_id,
                        target_id=class_id,
                        edge_type="inherits"
                    ))

        # Track class context for methods
        self.current_class_stack.append(ast_node.name)

        # Visit class body
        for item in ast_node.body:
            if isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                if item.name == "__init__":
                    # __init__ is part of the class definition - attach calls to class node
                    for child in ast.walk(item):
                        if isinstance(child, ast.Call):
                            self._handle_call(child, class_id)
                else:
                    self._visit_function(item, class_id, is_method=True)
            elif isinstance(item, ast.ClassDef):
                # Nested class
                self._visit_class(item, class_id)
            # Extract class-level calls (excluding functions which are handled above)
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._extract_calls_from_ast_node(item, class_id)

        self.current_class_stack.pop()

    def _visit_function(self, ast_node: ast.FunctionDef, parent_id: str, is_method: bool) -> None:
        """Visit a function or method definition."""
        if is_method:
            node_type = "Method"
            parent_class = self.current_class_stack[-1] if self.current_class_stack else None
            # For __init__, use the class name instead of __init__
            display_name = parent_class if (ast_node.name == "__init__" and parent_class) else ast_node.name
            func_id = self._make_id("Method", ast_node.name, self.current_file, parent_class)
        else:
            node_type = "Function"
            parent_class = None
            display_name = ast_node.name
            func_id = self._make_id("Function", ast_node.name, self.current_file)
            # Update defined_names for functions
            self.defined_names[ast_node.name] = func_id

        func_node = Node(
            id=func_id,
            node_type=node_type,
            name=display_name,
            file_path=self.current_file,
            line_start=ast_node.lineno,
            line_end=ast_node.end_lineno,
            is_callable=True,
            parent_class=parent_class
        )
        self.nodes.append(func_node)

        # Edge: parent contains this function/method
        self.edges.append(Edge(
            source_id=parent_id,
            target_id=func_id,
            edge_type="contains"
        ))

        # Extract calls within this function
        for child in ast.walk(ast_node):
            if isinstance(child, ast.Call):
                self._handle_call(child, func_id)

    def _extract_calls_from_ast_node(self, ast_node: ast.AST, caller_id: str) -> None:
        """Extract function calls from an AST node (for class-level or module-level code)."""
        # Skip function/class definitions - they're handled separately
        if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return

        for child in ast.walk(ast_node):
            if isinstance(child, ast.Call):
                self._handle_call(child, caller_id)

    def _handle_call(self, call_node: ast.Call, caller_id: str) -> None:
        """Handle a function call AST node."""
        func = call_node.func

        if isinstance(func, ast.Name):
            # Simple call: func_name()
            callee_name = func.id
            callee_id = self.defined_names.get(callee_name)

            if callee_id:
                # Check if it's a class (instantiation) or function (call).
                # `graph_node` here is one of OUR graph Nodes from self.nodes,
                # not an ast node — naming reflects that.
                graph_node = next((n for n in self.nodes if n.id == callee_id), None)
                if graph_node and graph_node.node_type == "Class":
                    self.edges.append(Edge(
                        source_id=caller_id,
                        target_id=callee_id,
                        edge_type="instantiates"
                    ))
                else:
                    self.edges.append(Edge(
                        source_id=caller_id,
                        target_id=callee_id,
                        edge_type="calls"
                    ))
            elif callee_name in self.imports:
                # Imported name - need to resolve via graph
                import_path = self.imports[callee_name]
                # Determine call type: if name starts with uppercase, likely a class instantiation
                call_type = "instantiates" if callee_name[0].isupper() else "calls"
                self.unresolved_calls.append(UnresolvedCall(
                    caller_id=caller_id,
                    callee_name=callee_name,
                    import_path=import_path,
                    call_type=call_type
                ))

        elif isinstance(func, ast.Attribute):
            # Method call: obj.method()
            method_name = func.attr
            resolved = False
            # Try to resolve the method locally — `graph_node` is our graph Node.
            for graph_node in self.nodes:
                if graph_node.node_type == "Method" and graph_node.name == method_name:
                    self.edges.append(Edge(
                        source_id=caller_id,
                        target_id=graph_node.id,
                        edge_type="calls"
                    ))
                    resolved = True
                    break

            # Check if it's a call on an imported module/class
            if not resolved and isinstance(func.value, ast.Name):
                obj_name = func.value.id
                if obj_name in self.imports:
                    # Module.func() or imported_class.method()
                    import_path = self.imports[obj_name]
                    full_callee = f"{obj_name}.{method_name}"
                    self.unresolved_calls.append(UnresolvedCall(
                        caller_id=caller_id,
                        callee_name=method_name,
                        import_path=import_path,
                        call_type="calls"
                    ))

    def _get_name_from_ast_node(self, ast_node: ast.AST) -> Optional[str]:
        """Extract a name string from an AST node (Name or Attribute)."""
        if isinstance(ast_node, ast.Name):
            return ast_node.id
        elif isinstance(ast_node, ast.Attribute):
            return ast_node.attr
        return None

    def _make_id(self, node_type: str, name: str, file_path: str = "", parent: str = None) -> str:
        """Generate a deterministic unique ID for a node."""
        if parent:
            return f"{node_type}:{file_path}:{parent}.{name}"
        return f"{node_type}:{file_path}:{name}"
