from src.parsers.python_parser import PythonParser
from src.datamodels import ParseResult, UnresolvedCall
from src.graph import GraphConnector
from src.llm import GraphQueryAgent
from typing import Dict, List, Optional
from pathlib import Path


LANGUAGE_DEFAULTS = {
    "python": {"uri": "bolt://localhost:7687", "extension": ".py", "parser_cls": PythonParser},
    "csharp": {"uri": "bolt://localhost:7688", "extension": ".cs", "parser_cls": None},
}


class CodeAnalyser():
    '''A class to analyse code for dependencies.'''

    def __init__(self, language: str = "python", graph_uri: Optional[str] = None):
        if language not in LANGUAGE_DEFAULTS:
            raise ValueError(
                f"Unsupported language: {language}. Choose from {list(LANGUAGE_DEFAULTS)}"
            )
        cfg = LANGUAGE_DEFAULTS[language]
        self.language = language
        self.extension = cfg["extension"]
        self.graph = GraphConnector(uri=graph_uri or cfg["uri"])
        self.parser = cfg["parser_cls"]() if cfg["parser_cls"] else None

    def perform(self,
                code: str,) -> ParseResult:
        '''Perform code analysis to count dependencies.'''
        if self.parser is None:
            print(f"Skipping: {self.language} parser not implemented yet")
            return ParseResult()
        parse_result = self.parser.parse_string(code)
        return parse_result

    def perform_path(self, dirpath: str, insert_to_graph: bool = True) -> Dict[str, int]:
        """Walk through a directory, parse all files matching the configured language,
        and optionally insert to graph.

        Args:
            dirpath: Path to directory to analyze
            insert_to_graph: Whether to insert results into the graph database

        Returns:
            Dict with stats: files_parsed, total_nodes, total_edges, resolved_calls, unresolved_calls
        """
        dirpath = Path(dirpath)
        if not dirpath.exists():
            raise ValueError(f"Directory does not exist: {dirpath}")

        exclude_dirs = {'__pycache__', '.git', 'venv', '.venv', 'node_modules', '.tox', 'env', '.ipynb_checkpoints'}
        candidate_files: List[Path] = []

        for path in dirpath.rglob(f'*{self.extension}'):
            if not any(excluded in path.parts for excluded in exclude_dirs):
                candidate_files.append(path)

        stats = {"files_parsed": 0, "total_nodes": 0, "total_edges": 0, "resolved_calls": 0, "unresolved_calls": 0}

        if self.parser is None:
            for file_path in candidate_files:
                print(f"Skipping {file_path}: {self.language} parser not implemented yet")
            return stats

        all_unresolved: List[UnresolvedCall] = []

        # Phase 1: Parse all files and insert nodes/edges
        for file_path in candidate_files:
            try:
                result = self.parser.parse_file(str(file_path))
                stats["files_parsed"] += 1
                stats["total_nodes"] += len(result.nodes)
                stats["total_edges"] += len(result.edges)

                all_unresolved.extend(result.unresolved_calls)

                if insert_to_graph:
                    self.graph.insert_parse_result(result)

            except Exception as e:
                print(f"Error parsing {file_path}: {e}")

        # Phase 2: Resolve cross-file calls now that all nodes are in the graph
        if insert_to_graph and all_unresolved:
            for unresolved in all_unresolved:
                if self.graph.resolve_unresolved_call(unresolved):
                    stats["resolved_calls"] += 1
                else:
                    stats["unresolved_calls"] += 1

        return stats



    def perform_file(self, inpath: str, insert_to_graph: bool = True) -> ParseResult:
        """Perform code analysis on a single file.

        Args:
            inpath: The path to the code file to be analysed.
            insert_to_graph: Whether to insert results into the graph database

        Returns:
            ParseResult with nodes and edges
        """
        if self.parser is None:
            print(f"Skipping {inpath}: {self.language} parser not implemented yet")
            return ParseResult()

        result = self.parser.parse_file(inpath)

        if insert_to_graph:
            self.graph.insert_parse_result(result)

        return result

    def auxiliary_endpoint(self):
        return {'/file': self.perform_file,
                '/directory': self.perform_path}
    
    def close(self):
        """Close the graph connection."""
        self.graph.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    

    def __detect_language__(self, code: str) -> str:
        '''Detect the programming language of the given code snippet.
        
        for now can do Python and C#'''
        # C# indicators
        csharp_patterns = ['using System', 'namespace ', 'public class', 'private ', 'protected ', '};', 'void ']
        
        # Python indicators  
        python_patterns = ['def ', 'import ', 'from ', 'self.', 'elif ', ':\n', '__init__']
        
        csharp_score = sum(1 for p in csharp_patterns if p in code)
        python_score = sum(1 for p in python_patterns if p in code)
        
        if csharp_score > python_score:
            return 'csharp'
        elif python_score > csharp_score:
            return 'python'
        return 'unknown'
    
    def __detect_language_file__(self, inpath: str) -> str:
        '''Detect the programming language of a code file, based on file extension.'''
        ext = inpath.split('.')[-1].lower()
        return {'py': 'python', 'cs': 'csharp'}.get(ext, 'unknown')
    
    def create_query_agent(self, deployment: str = None) -> GraphQueryAgent:
        """Create an LLM-powered agent for natural language graph queries.
        
        Args:
            deployment: Azure OpenAI deployment name (defaults to env var)
            
        Returns:
            GraphQueryAgent instance
        """
        return GraphQueryAgent(self.graph, deployment)
    
    def ask(self, question: str, execute: bool = True) -> Dict:
        """Quick method to ask a natural language question about the codebase.
        
        Args:
            question: Natural language question (e.g., "What classes call GraphConnector?")
            execute: Whether to execute the query or just generate it
            
        Returns:
            Dict with 'question', 'query', 'results', and 'error' keys
        """
        if not hasattr(self, '_query_agent'):
            self._query_agent = self.create_query_agent()
        return self._query_agent.query(question, execute)