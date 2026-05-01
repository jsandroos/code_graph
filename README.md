# Code Analyzer

An AST-grounded code knowledge graph for LLM coding agents.

The tool parses a Python codebase, extracts files, classes, functions, and
methods as nodes, wires up `CONTAINS` / `CALLS` / `INSTANTIATES` / `INHERITS`
edges, and enriches each callable with a uniformly formatted Google-style
docstring whose embedding can later be used as semantic memory for an LLM
agent. Structure comes from the AST (deterministic, cheap, and exact);
semantics come from LLM-generated docstrings layered on top.

## Vision

Two complementary use cases:

1. **Insights / teaching tool.** A human queries the graph (via Memgraph Lab
   or natural language) to understand an unfamiliar codebase quickly.
2. **LLM coding-agent memory.** An agent uses the graph as a router-to-source:
   structural questions ("what calls X?", "what's the dependency closure of
   module Y?") collapse into exact Cypher queries; semantic questions ("find
   code related to retry logic") fall back on docstring embeddings stored as
   node properties. The agent fetches actual source on demand using the
   `(file_path, line_start, line_end)` recorded on each node.

## Main program flow

```
1. perform_path(directory)               # AST parse: nodes + edges
       ↓
2. cross-file call resolution            # in-graph Cypher MATCH
       ↓
3. enrich_docstrings(parse_result)       # LLM docstring per callable, post-AST
       ↓
4. (future) embed_docstrings()           # vector property per node
       ↓
5. (future) graph validation pass        # consistency check across hashes
```

Each step writes to the graph and is independently re-runnable. The flow is
deliberately split: the AST pass is fast and free, while the LLM passes are
slow and cost tokens, so they run separately and only re-do work for nodes
whose source has changed.

## Design decisions

### Per-language Memgraphs

The graph backend is split per source language (one Memgraph instance per
language) to keep each subgraph small and queries language-pure. Defaults:

| Language | Bolt URI                  | Container         |
|----------|---------------------------|-------------------|
| python   | `bolt://localhost:7687`   | `memgraph-python` |
| csharp   | `bolt://localhost:7688`   | `memgraph-csharp` |

`CodeAnalyser(language="python")` selects the Python graph; `language="csharp"`
selects the C# graph (parser not yet implemented; the walker logs and skips).

### Docstring generation runs *after* the AST pass

The LLM docstring generator operates on AST blocks (Function / Method / Class)
already extracted by the parser, not on raw files. This keeps the parsing
phase fast and deterministic, and lets the enrichment pass be re-run
independently — for example, after upgrading the prompt or model, without
re-parsing the codebase.

The alternative (running an LLM "is this documented?" check on every file
during ingest) was considered and rejected: it would conflate two very
different concerns (structure vs. natural-language quality) into one slow
pipeline, and would re-do AST work each time the LLM step is iterated on.

### No augmentation of human docstrings (for now)

When the parser encounters a function that already carries a human-written
docstring, the enrichment pass currently **ignores the existing source
docstring** and decides solely from the graph state whether to generate. The
reason is determinism: LLM interpretations of free-form prose vary across
runs, so reusing or "normalizing" an existing docstring introduces noise into
the embedding pipeline. A clean LLM regeneration from source is repeatable.

This will become the responsibility of a later **graph validation step**:
on each enrichment run, the generator hashes the current source segment and
compares against the `code_hash` stored on the matching graph node:

- No node / no docstring → generate.
- `code_hash` mismatch → regenerate (source has drifted).
- `code_hash` matches and a docstring is present → skip.

The same pattern will extend to the embedding layer (`docstring_hash` →
re-embed) and to manual edits / corruption (`embedding_vector_hash`).

### Node schema

Per-node properties (set by the parser unless noted):

| Property               | Set by         | Notes                                           |
|------------------------|----------------|-------------------------------------------------|
| `id`                   | parser         | `{node_type}:{file_path}:{name}` (deterministic) |
| `node_type`            | parser         | `File` / `Class` / `Function` / `Method`        |
| `name`, `file_path`    | parser         |                                                 |
| `line_start`, `line_end` | parser       | Used to read source on demand                   |
| `is_callable`, `parent_class` | parser  |                                                 |
| `docstring`            | enrichment     | Google-style, generated from source             |
| `docstring_hash`       | enrichment     | SHA-256 of `docstring`                          |
| `code_hash`            | enrichment     | SHA-256 of the source segment used to generate  |
| `embedding_vector`     | future         | Vector embedding of `docstring`                 |
| `embedding_vector_hash`| future         | Integrity check against post-write corruption   |
| `embedding_model`      | future         | Model id (e.g. `text-embedding-3-large`)        |

## Setup

```bash
# Bring up Memgraphs (one per language)
docker compose -f docker-memgraph.yml up -d

# Build the dev container (installs neo4j, openai, pythonnet)
make docker-build-dev
make start-bash-dev
```

Inside the container:

```python
from code_analyser import CodeAnalyser

with CodeAnalyser(language="python") as ca:
    parse_stats = ca.perform_path("./src")
    enrich_stats = ca.enrich_docstrings(
        # perform_path returns aggregate stats; for enrichment, parse a single
        # file or call perform_file to get a ParseResult to feed in.
        ca.perform_file("./code_analyser.py")
    )
    print(parse_stats, enrich_stats)
```

## Format requirements for generated docstrings

- Google style (`Args:` / `Returns:` / `Yields:` / `Raises:` / `Attributes:`).
- PEP 257 / PEP 8 compliant.
- 88-character soft wrap (black default).
- Triple double-quotes.
- Sphinx-readable via `sphinx.ext.napoleon`.

The generator wraps and indents the LLM output to match each insertion site;
generated docstrings are also written back into the source `.py` file as the
first statement of the block (replacing any existing docstring), with the
file re-parsed for syntactic validity before the write is committed.

## Status

| Phase                                              | Status        |
|----------------------------------------------------|---------------|
| 1. Files / classes / functions / methods as nodes  | Done          |
| 2. Calls + dependencies as edges                   | Done          |
| 3a. Docstring enrichment per node                  | Done (Python) |
| 3b. Embedding per node                             | Planned       |
| 3c. LLM tool surface (MCP / skills)                | Planned       |
| 4. C# parser                                       | Planned       |
| 5. Graph validation pass                           | Planned       |
