"""Memgraph database connection."""

from neo4j import GraphDatabase
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.datamodels import Node, Edge, ParseResult, UnresolvedCall


class GraphConnector:
    """Connection to Memgraph database.

    Pure CRUD adapter — knows nothing about docstrings, embeddings, or any
    enrichment-specific concept. Higher-level orchestrators (e.g.
    DocstringGenerator) compose `set_node_properties` / `get_node_properties`
    to read and write enrichment state.
    """

    def __init__(self, uri: str = "bolt://localhost:7687", user: str = "", password: str = ""):
        """Initialize connection to Memgraph."""
        try:
            if user and password:
                self.driver = GraphDatabase.driver(uri, auth=(user, password))
            else:
                self.driver = GraphDatabase.driver(uri)
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
        """Insert or update a node using MERGE (idempotent).

        Only writes enrichment fields (docstring, hashes) when they are non-null,
        so that re-parsing does not clobber a previously generated docstring.
        """
        cypher = f"""
        MERGE (n:{node.node_type} {{id: $id}})
        SET n.name = $name,
            n.file_path = $file_path,
            n.line_start = $line_start,
            n.line_end = $line_end,
            n.is_callable = $is_callable,
            n.parent_class = $parent_class
        """
        params = {
            "id": node.id,
            "name": node.name,
            "file_path": node.file_path,
            "line_start": node.line_start,
            "line_end": node.line_end,
            "is_callable": node.is_callable,
            "parent_class": node.parent_class,
        }
        if node.docstring is not None:
            cypher += ", n.docstring = $docstring, n.docstring_hash = $docstring_hash, n.code_hash = $code_hash"
            params["docstring"] = node.docstring
            params["docstring_hash"] = node.docstring_hash
            params["code_hash"] = node.code_hash
        self.execute(cypher, params)

    def get_node_properties(self, node_id: str, props: List[str]) -> Optional[dict]:
        """Read a selected set of properties from a node by id.

        Generic primitive used by enrichment layers (docstring, embedding, ...)
        to read whatever subset of fields they care about. Returns None if the
        node does not exist; otherwise a dict mapping each requested property
        name to its current value (or None if the property is absent).
        """
        if not props:
            return None
        return_clause = ", ".join(f"n.{p} AS {p}" for p in props)
        results = self.query(
            f"MATCH (n {{id: $id}}) RETURN {return_clause}",
            {"id": node_id},
        )
        return results[0] if results else None

    def set_node_properties(self, node_id: str, props: dict) -> None:
        """Set a dict of properties on a node by id (no-op if dict is empty)."""
        if not props:
            return
        set_clause = ", ".join(f"n.{k} = ${k}" for k in props)
        params = {"id": node_id, **props}
        self.execute(
            f"MATCH (n {{id: $id}}) SET {set_clause}",
            params,
        )

    def insert_edge(self, edge: "Edge") -> None:
        """Insert an edge using MERGE (idempotent)."""
        cypher = f"""
        MATCH (source {{id: $source_id}})
        MATCH (target {{id: $target_id}})
        MERGE (source)-[r:{edge.edge_type.upper()}]->(target)
        """
        self.execute(cypher, {
            "source_id": edge.source_id,
            "target_id": edge.target_id
        })

    def insert_parse_result(self, result: "ParseResult") -> dict:
        """Insert all nodes and edges from a ParseResult. Returns stats."""
        nodes_inserted = 0
        edges_inserted = 0

        for node in result.nodes:
            self.insert_node(node)
            nodes_inserted += 1

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
            "MATCH (n) WHERE n.name = $name AND n.file_path = $file_path RETURN n.id AS id, labels(n)[0] AS node_type",
            {"name": name, "file_path": file_path}
        )
        return results[0] if results else None

    def find_node_by_name(self, name: str, node_types: List[str] = None) -> List[dict]:
        """Find nodes by name, optionally filtering by type."""
        if node_types:
            label_conditions = " OR ".join([f"n:{t}" for t in node_types])
            cypher = f"MATCH (n) WHERE n.name = $name AND ({label_conditions}) RETURN n.id AS id, labels(n)[0] AS node_type, n.file_path AS file_path"
        else:
            cypher = "MATCH (n) WHERE n.name = $name RETURN n.id AS id, labels(n)[0] AS node_type, n.file_path AS file_path"
        return self.query(cypher, {"name": name})

    def resolve_import(self, import_path: str, callee_name: str) -> Optional[str]:
        """Resolve an import statement to a target node id.

        Args:
            import_path: Module path from the import (e.g., 'src.graph').
            callee_name: The name being called (e.g., 'GraphConnector').

        Returns:
            Node id when found, otherwise None.
        """
        module_path = import_path.replace(".", "/") + ".py"

        results = self.query(
            """
            MATCH (n)
            WHERE n.name = $name AND n.file_path ENDS WITH $module_path
            RETURN n.id AS id, labels(n)[0] AS node_type
            """,
            {"name": callee_name, "module_path": module_path}
        )

        if results:
            return results[0]["id"]

        init_path = import_path.replace(".", "/") + "/__init__.py"
        results = self.query(
            """
            MATCH (n)
            WHERE n.name = $name AND n.file_path ENDS WITH $init_path
            RETURN n.id AS id, labels(n)[0] AS node_type
            """,
            {"name": callee_name, "init_path": init_path}
        )

        if results:
            return results[0]["id"]

        results = self.find_node_by_name(callee_name, ["Class", "Function"])
        if len(results) == 1:
            return results[0]["id"]

        return None

    def resolve_unresolved_call(self, unresolved: "UnresolvedCall") -> bool:
        """Resolve an unresolved call and insert the corresponding edge.

        Returns:
            True when a target was found and the edge was inserted, False otherwise.
        """
        target_id = self.resolve_import(unresolved.import_path, unresolved.callee_name)

        if target_id:
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
