from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Node:
    """Represents a code entity."""
    id: str
    type: str  # 'File', 'Class', 'Function', 'Method'
    name: str
    file_path: str
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    callable: bool = False
    parent_class: Optional[str] = None


@dataclass
class Edge:
    """Represents a relationship between code entities."""
    source_id: str
    target_id: str
    type: str  # 'contains', 'calls', 'instantiates', 'inherits'


@dataclass
class UnresolvedCall:
    """Represents a call that couldn't be resolved locally (cross-file)."""
    caller_id: str
    callee_name: str
    import_path: Optional[str]  # e.g., "src.graph.GraphConnector" or None if not imported
    call_type: str  # 'calls' or 'instantiates'


@dataclass
class ParseResult:
    """Result of parsing a file or codebase."""
    nodes: List[Node] = field(default_factory=list)
    edges: List[Edge] = field(default_factory=list)
    unresolved_calls: List[UnresolvedCall] = field(default_factory=list)