"""Memgraph database connection."""

from neo4j import GraphDatabase
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.datamodels import Node, Edge, ParseResult, UnresolvedCall


class GraphConnector:
    """Connection to Memgraph database."""
    
    def __init__(self, uri: str = "bolt://localhost:7687", user: str = "", password: str = ""):
        """Initialize connection to Memgraph."""
        try:
            if user and password:
                self.driver = GraphDatabase.driver(uri, auth=(user, password))
            else:
                self.driver = GraphDatabase.driver(uri)
            # Test the connection
            self.test()
        except Exception as e:
            raise ConnectionError(f"Failed to connect to Memgraph at {uri}: {str(e)}")

    def test(self):
        """Test the connection."""
        with self.driver.session() as session:
            result = session.run("RETURN 1 AS test")
            return result.single()["test"] == 1

    def close(self):
        """Close the connection."""
        self.driver.close()
    
    def query(self, cypher: str, parameters: dict = None):
        """Execute a Cypher query and return results."""
        with self.driver.session() as session:
            result = session.run(cypher, parameters or {})
            return [record.data() for record in result]
    
    def execute(self, cypher: str, parameters: dict = None):
        """Execute a Cypher query without returning results."""
        with self.driver.session() as session:
            session.run(cypher, parameters or {})
    
    def insert_node(self, node: "Node") -> None:
        """Insert or update a node using MERGE (idempotent)."""
        cypher = f"""
        MERGE (n:{node.type} {{id: $id}})
        SET n.name = $name,
            n.file_path = $file_path,
            n.line_start = $line_start,
            n.line_end = $line_end,
            n.callable = $callable,
            n.parent_class = $parent_class
        """
        self.execute(cypher, {
            "id": node.id,
            "name": node.name,
            "file_path": node.file_path,
            "line_start": node.line_start,
            "line_end": node.line_end,
            "callable": node.callable,
            "parent_class": node.parent_class
        })
    
    def insert_edge(self, edge: "Edge") -> None:
        """Insert an edge using MERGE (idempotent)."""
        cypher = f"""
        MATCH (source {{id: $source_id}})
        MATCH (target {{id: $target_id}})
        MERGE (source)-[r:{edge.type.upper()}]->(target)
        """
        self.execute(cypher, {
            "source_id": edge.source_id,
            "target_id": edge.target_id
        })
    
    def insert_parse_result(self, result: "ParseResult") -> dict:
        """Insert all nodes and edges from a ParseResult. Returns stats."""
        nodes_inserted = 0
        edges_inserted = 0
        
        # Insert nodes first
        for node in result.nodes:
            self.insert_node(node)
            nodes_inserted += 1
        
        # Then insert edges
        for edge in result.edges:
            self.insert_edge(edge)
            edges_inserted += 1
        
        return {"nodes": nodes_inserted, "edges": edges_inserted}
    
    def get_stats(self) -> dict:
        """Get count of nodes and edges in the database."""
        nodes = self.query("MATCH (n) RETURN count(n) AS count")[0]["count"]
        edges = self.query("MATCH ()-[r]->() RETURN count(r) AS count")[0]["count"]
        return {"nodes": nodes, "edges": edges}
    
    def find_node_by_name_and_file(self, name: str, file_path: str) -> Optional[dict]:
        """Find a node by name and file path."""
        results = self.query(
            "MATCH (n) WHERE n.name = $name AND n.file_path = $file_path RETURN n.id AS id, labels(n)[0] AS type",
            {"name": name, "file_path": file_path}
        )
        return results[0] if results else None
    
    def find_node_by_name(self, name: str, node_types: List[str] = None) -> List[dict]:
        """Find nodes by name, optionally filtering by type."""
        if node_types:
            # Create dynamic label match
            label_conditions = " OR ".join([f"n:{t}" for t in node_types])
            cypher = f"MATCH (n) WHERE n.name = $name AND ({label_conditions}) RETURN n.id AS id, labels(n)[0] AS type, n.file_path AS file_path"
        else:
            cypher = "MATCH (n) WHERE n.name = $name RETURN n.id AS id, labels(n)[0] AS type, n.file_path AS file_path"
        return self.query(cypher, {"name": name})
    
    def resolve_import(self, import_path: str, callee_name: str) -> Optional[str]:
        """
        Resolve an import to find the target node ID.
        
        import_path: The module path from the import statement (e.g., 'src.graph' or 'src.parsers.python_parser')
        callee_name: The name being called (e.g., 'GraphConnector' or 'PythonParser')
        
        Returns the node ID if found, None otherwise.
        """
        # Convert import path to potential file paths
        # e.g., 'src.graph' -> 'src/graph.py'
        # e.g., 'src.parsers.python_parser' -> 'src/parsers/python_parser.py'
        module_path = import_path.replace(".", "/") + ".py"
        
        # Try to find a node with this name in a file matching the module path
        results = self.query(
            """
            MATCH (n) 
            WHERE n.name = $name AND n.file_path ENDS WITH $module_path
            RETURN n.id AS id, labels(n)[0] AS type
            """,
            {"name": callee_name, "module_path": module_path}
        )
        
        if results:
            return results[0]["id"]
        
        # Try alternative: maybe the import is from a package __init__.py
        init_path = import_path.replace(".", "/") + "/__init__.py"
        results = self.query(
            """
            MATCH (n) 
            WHERE n.name = $name AND n.file_path ENDS WITH $init_path
            RETURN n.id AS id, labels(n)[0] AS type
            """,
            {"name": callee_name, "init_path": init_path}
        )
        
        if results:
            return results[0]["id"]
        
        # Fallback: search by name only (may have multiple matches)
        results = self.find_node_by_name(callee_name, ["Class", "Function"])
        if len(results) == 1:
            return results[0]["id"]
        
        return None
    
    def resolve_unresolved_call(self, unresolved: "UnresolvedCall") -> bool:
        """
        Attempt to resolve an unresolved call and insert the edge if successful.
        Returns True if resolved, False otherwise.
        """
        target_id = self.resolve_import(unresolved.import_path, unresolved.callee_name)
        
        if target_id:
            # Create the edge
            cypher = f"""
            MATCH (source {{id: $source_id}})
            MATCH (target {{id: $target_id}})
            MERGE (source)-[r:{unresolved.call_type.upper()}]->(target)
            """
            self.execute(cypher, {
                "source_id": unresolved.caller_id,
                "target_id": target_id
            })
            return True
        
        return False
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()