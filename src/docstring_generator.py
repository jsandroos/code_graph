"""Docstring generation and source-file insertion.

Operates on a single AST block (Function / Method / Class) at a time. Produces a
Google-style docstring via Azure OpenAI, then inserts it into the .py source as
the first statement of the block (replacing an existing docstring if one is
present).

Format requirements:
    - Google style (Args / Returns / Raises / Attributes sections)
    - PEP 257 / PEP 8 compliant
    - black-compatible (88-char soft wrap, triple double-quotes)
    - Sphinx-readable via sphinx.ext.napoleon

The class exposes static methods only — no instance state. The Azure OpenAI
client is built lazily on first use via src.llm.LLM.
"""

from __future__ import annotations

import ast
import hashlib
import os
import textwrap
from pathlib import Path
from typing import Dict, Optional, TYPE_CHECKING

from src.datamodels import Node, ParseResult
from src.llm import LLM

if TYPE_CHECKING:
    from src.graph import GraphConnector


SYSTEM_PROMPT = """You are a senior Python engineer producing reference docstrings for a code knowledge graph.

Output a single Google-style docstring for the supplied Python {node_type}. The docstring will be embedded as a vector and used by an LLM agent to navigate the codebase, so it must be concise, factual, and uniformly structured across the whole codebase.

Strict format requirements:
- Google style with these sections, in this order, omitting any that do not apply: Args, Returns, Yields, Raises, Attributes (classes only).
- First line: imperative-mood summary, ends with a period, fits within 88 characters total (including leading indent at insertion time).
- Blank line, then optional 1-3 sentence extended description.
- Section headers exactly: "Args:", "Returns:", "Yields:", "Raises:", "Attributes:".
- Section bodies indented 4 spaces from the header.
- Each Args entry: "name (type): description." Use the type hint when present, otherwise omit the parenthesised type.
- 88-character soft wrap, PEP 257 / PEP 8 compliant, sphinx.ext.napoleon-readable.

Output rules:
- Return ONLY the docstring body. Do NOT wrap in triple quotes. Do NOT include code fences. Do NOT include the function or class signature.
- No commentary, no preamble, no trailing notes.
- If the source is unparseable or empty, return: ERROR: <one-line reason>.
"""


USER_TEMPLATE = """Document this Python {node_type} named `{name}`.

Source:
```python
{source}
```
"""


class DocstringGenerator:
    """Generate Google-style docstrings and insert them into Python source files."""

    _client = None

    @classmethod
    def _get_client(cls):
        if cls._client is None:
            cls._client = LLM()
        return cls._client

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------

    @staticmethod
    def hash_text(text: str) -> str:
        """Return a stable SHA-256 hex digest of the given text."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Source extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_source(node: Node) -> Optional[str]:
        """Return the exact source text for a Node, read fresh from disk.

        Returns None for File nodes (no source segment) or when the file is
        missing or unparseable.
        """
        if node.node_type == "File":
            return None
        if node.line_start is None or node.line_end is None:
            return None
        path = Path(node.file_path)
        if not path.exists():
            return None
        try:
            full_source = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            tree = ast.parse(full_source)
        except SyntaxError:
            return None
        ast_block = DocstringGenerator._find_ast_block(tree, node)
        if ast_block is None:
            return None
        return ast.get_source_segment(full_source, ast_block)

    @staticmethod
    def _find_ast_block(tree: ast.Module, node: Node) -> Optional[ast.AST]:
        """Locate the AST block matching a Node by name + line range."""
        target_types = {
            "Class": (ast.ClassDef,),
            "Function": (ast.FunctionDef, ast.AsyncFunctionDef),
            "Method": (ast.FunctionDef, ast.AsyncFunctionDef),
        }.get(node.node_type)
        if not target_types:
            return None
        for ast_node in ast.walk(tree):
            if (
                isinstance(ast_node, target_types)
                and ast_node.name == node.name
                and ast_node.lineno == node.line_start
            ):
                return ast_node
        return None

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    @classmethod
    def generate(cls, node: Node, source: str) -> str:
        """Produce a Google-style docstring body for the given Node.

        The returned string is the docstring CONTENT only — no triple quotes,
        no surrounding indentation. Insertion handles wrapping.
        """
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
        client = cls._get_client()
        node_type_label = node.node_type.lower()
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.format(node_type=node_type_label)},
                {"role": "user", "content": USER_TEMPLATE.format(
                    node_type=node_type_label, name=node.name, source=source,
                )},
            ],
            temperature=0,
            max_tokens=600,
        )
        body = response.choices[0].message.content.strip()
        # Strip stray triple quotes if the model included them anyway.
        if body.startswith('"""'):
            body = body[3:]
        if body.endswith('"""'):
            body = body[:-3]
        return body.strip()

    # ------------------------------------------------------------------
    # Source-file insertion
    # ------------------------------------------------------------------

    @staticmethod
    def insert_into_source(node: Node, docstring_body: str) -> bool:
        """Insert (or replace) a docstring at the top of the Node's body in source.

        The file is parsed with `ast` to locate the block and any existing
        docstring expression. The new docstring is wrapped in triple double-quotes
        and indented to match the block's body. The final file is re-parsed before
        being written; if it would be invalid Python, the write is aborted.

        Returns True on success, False if anything prevented the write.
        """
        path = Path(node.file_path)
        if not path.exists():
            return False
        original = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(original)
        except SyntaxError:
            return False
        ast_block = DocstringGenerator._find_ast_block(tree, node)
        if ast_block is None or not getattr(ast_block, "body", None):
            return False

        body_indent = " " * (ast_block.col_offset + 4)
        wrapped = DocstringGenerator._wrap_docstring(docstring_body, body_indent)

        lines = original.splitlines(keepends=True)

        first_stmt = ast_block.body[0]
        existing_docstring = (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        )

        if existing_docstring:
            start_line = first_stmt.lineno - 1
            end_line = first_stmt.end_lineno
            new_lines = lines[:start_line] + [wrapped] + lines[end_line:]
        else:
            insert_line = first_stmt.lineno - 1
            new_lines = lines[:insert_line] + [wrapped] + lines[insert_line:]

        new_source = "".join(new_lines)
        try:
            ast.parse(new_source)
        except SyntaxError:
            return False
        path.write_text(new_source, encoding="utf-8")
        return True

    @staticmethod
    def _wrap_docstring(body: str, indent: str) -> str:
        """Wrap a docstring body in triple double-quotes with proper indentation.

        Returns a single string ending in a newline so it can be spliced into a
        list of file lines as a drop-in replacement. Single-line docstrings stay
        on one line; multi-line docstrings open and close on their own lines per
        PEP 257.
        """
        body = body.strip()
        body_lines = body.splitlines()
        if len(body_lines) <= 1:
            return f'{indent}"""{body}"""\n'
        first, rest = body_lines[0], body_lines[1:]
        indented_rest = textwrap.indent("\n".join(rest), indent)
        return f'{indent}"""{first}\n{indented_rest}\n{indent}"""\n'

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    @classmethod
    def enrich(
        cls,
        parse_result: ParseResult,
        graph: "GraphConnector",
        write_to_source: bool = True,
    ) -> Dict[str, int]:
        """Generate Google-style docstrings for nodes lacking an up-to-date one.

        For each non-File node in `parse_result`, the current source segment is
        hashed and compared against the `code_hash` stored on the matching graph
        node (read via the generic `get_node_properties` primitive). If the
        stored hash matches and a docstring is already present, the node is
        skipped. Otherwise the docstring is generated via the LLM, optionally
        inserted into the .py source file, and persisted to the graph along
        with both hashes.

        Args:
            parse_result: The ParseResult returned by perform_path / perform_file.
            graph: A connected GraphConnector. Used only for property reads/writes.
            write_to_source: If True (default), the generated docstring is also
                inserted into the underlying .py file. If False, only graph
                state is updated.

        Returns:
            Dict with keys: candidates, generated, skipped_up_to_date,
            skipped_no_source, skipped_files, source_writes, source_write_failures,
            llm_failures.
        """
        stats = {
            "candidates": 0,
            "generated": 0,
            "skipped_up_to_date": 0,
            "skipped_no_source": 0,
            "skipped_files": 0,
            "source_writes": 0,
            "source_write_failures": 0,
            "llm_failures": 0,
        }
        for node in parse_result.nodes:
            if node.node_type == "File":
                stats["skipped_files"] += 1
                continue
            stats["candidates"] += 1

            source = cls.extract_source(node)
            if source is None:
                stats["skipped_no_source"] += 1
                continue

            code_hash = cls.hash_text(source)
            existing = graph.get_node_properties(
                node.id, ["docstring", "code_hash"]
            )
            if existing and existing.get("docstring") and existing.get("code_hash") == code_hash:
                stats["skipped_up_to_date"] += 1
                continue

            try:
                body = cls.generate(node, source)
            except Exception as exc:
                print(f"LLM failure on {node.id}: {exc}")
                stats["llm_failures"] += 1
                continue
            if body.startswith("ERROR:"):
                print(f"LLM declined docstring for {node.id}: {body}")
                stats["llm_failures"] += 1
                continue

            if write_to_source:
                if cls.insert_into_source(node, body):
                    stats["source_writes"] += 1
                else:
                    stats["source_write_failures"] += 1

            graph.set_node_properties(
                node.id,
                {
                    "docstring": body,
                    "docstring_hash": cls.hash_text(body),
                    "code_hash": code_hash,
                },
            )
            stats["generated"] += 1

        return stats
