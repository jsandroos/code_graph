import os
from openai import AzureOpenAI
from typing import List, Dict, Any, Optional

def LLM(batch_size=100):
    """Use regular OpenAI for embeddings"""
    endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT')
    api_version = os.getenv('AZURE_OPENAI_VERSION')
    api_key = os.getenv('AZURE_OPENAI_API_KEY')
    client = AzureOpenAI(
                    azure_endpoint=endpoint,
                    #azure_deployment=deployment,
                    api_version=api_version,
                    api_key=api_key,)
    return client


# Graph schema documentation for the LLM
GRAPH_SCHEMA = """
## Graph Database Schema

### Node Types

1. **File**
   - Represents a Python source file
   - Properties:
     - `id`: Unique identifier (format: "File:{file_path}:{file_name}")
     - `name`: File name (e.g., "graph.py")
     - `file_path`: Full path to the file
     - `line_start`: Always null for files
     - `line_end`: Always null for files
     - `is_callable`: Always false
     - `parent_class`: Always null

2. **Class**
   - Represents a Python class definition
   - Properties:
     - `id`: Unique identifier (format: "Class:{file_path}:{class_name}")
     - `name`: Class name (e.g., "GraphConnector")
     - `file_path`: Path to the file containing this class
     - `line_start`: Line number where class definition starts
     - `line_end`: Line number where class definition ends
     - `is_callable`: Always false
     - `parent_class`: Always null

3. **Function**
   - Represents a top-level function (not inside a class)
   - Properties:
     - `id`: Unique identifier (format: "Function:{file_path}:{func_name}")
     - `name`: Function name
     - `file_path`: Path to the file containing this function
     - `line_start`: Line number where function starts
     - `line_end`: Line number where function ends
     - `is_callable`: Always true
     - `parent_class`: Always null

4. **Method**
   - Represents a method inside a class (excluding __init__)
   - Properties:
     - `id`: Unique identifier (format: "Method:{file_path}:{class_name}.{method_name}")
     - `name`: Method name
     - `file_path`: Path to the file containing this method
     - `line_start`: Line number where method starts
     - `line_end`: Line number where method ends
     - `is_callable`: Always true
     - `parent_class`: Name of the containing class

### Edge Types

1. **CONTAINS**
   - Direction: Parent -> Child
   - Usage: File CONTAINS Class/Function, Class CONTAINS Method
   - Example: `(file:File)-[:CONTAINS]->(class:Class)`

2. **CALLS**
   - Direction: Caller -> Callee
   - Usage: When a function/method/class calls another function/method
   - Example: `(method:Method)-[:CALLS]->(func:Function)`

3. **INSTANTIATES**
   - Direction: Caller -> Class
   - Usage: When code creates an instance of a class
   - Example: `(method:Method)-[:INSTANTIATES]->(class:Class)`

4. **INHERITS**
   - Direction: Bidirectional (edges exist in both directions)
   - Usage: Class inheritance relationships
   - Example: `(child:Class)-[:INHERITS]->(parent:Class)` and `(parent:Class)-[:INHERITS]->(child:Class)`

### Important Notes

- __init__ methods are NOT separate nodes; their calls are attached to the Class node
- Node IDs are deterministic and based on file path and name
- Use MATCH with label for better performance (e.g., `MATCH (c:Class)` instead of `MATCH (c)`)
- Edge types are uppercase in queries (CONTAINS, CALLS, INSTANTIATES, INHERITS)
"""

SYSTEM_PROMPT = """You are an expert in Cypher query language for graph databases, specifically Memgraph.
Your role is to translate natural language questions about a codebase into Cypher queries.

{schema}

### Query Guidelines

1. Always use node labels when matching (e.g., `MATCH (c:Class)` not `MATCH (c)`)
2. Use relationship types in uppercase (CONTAINS, CALLS, INSTANTIATES, INHERITS)
3. For finding dependencies, trace CALLS and INSTANTIATES edges
4. For finding structure, trace CONTAINS edges
5. Use `OPTIONAL MATCH` when relationships might not exist
6. Return meaningful aliases for readability
7. Use `DISTINCT` to avoid duplicates when traversing multiple paths

### Response Format

Respond with ONLY the Cypher query, no explanations or markdown formatting.
If you cannot create a valid query, respond with: ERROR: <reason>

### Example Queries

Q: "What classes are in the graph?"
A: MATCH (c:Class) RETURN c.name AS class_name, c.file_path AS file

Q: "What does the GraphConnector class instantiate?"
A: MATCH (c:Class {{name: 'GraphConnector'}})-[:INSTANTIATES]->(target) RETURN target.name AS instantiated, labels(target)[0] AS node_type

Q: "Show all methods in the PythonParser class"
A: MATCH (c:Class {{name: 'PythonParser'}})-[:CONTAINS]->(m:Method) RETURN m.name AS method_name

Q: "What functions does perform_path call?"
A: MATCH (m:Method {{name: 'perform_path'}})-[:CALLS|INSTANTIATES]->(target) RETURN target.name AS called, labels(target)[0] AS node_type

Q: "Find all classes that inherit from another class"
A: MATCH (child:Class)-[:INHERITS]->(parent:Class) RETURN child.name AS child_class, parent.name AS parent_class
"""


class GraphQueryAgent:
    """LLM-powered agent for querying the code graph using natural language."""
    
    def __init__(self, graph_connector, deployment: str = None):
        """
        Initialize the query agent.
        
        Args:
            graph_connector: GraphConnector instance for executing queries
            deployment: Azure OpenAI deployment name (defaults to env var)
        """
        self.graph = graph_connector
        self.client = LLM()
        self.deployment = deployment or os.getenv('AZURE_OPENAI_DEPLOYMENT')
        self.system_prompt = SYSTEM_PROMPT.format(schema=GRAPH_SCHEMA)
        self.conversation_history: List[Dict[str, str]] = []
    
    def _call_llm(self, user_message: str, include_history: bool = False) -> str:
        """Call the LLM with the given message."""
        messages = [{"role": "system", "content": self.system_prompt}]
        
        if include_history:
            messages.extend(self.conversation_history)
        
        messages.append({"role": "user", "content": user_message})
        
        response = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            temperature=0,  # Deterministic for query generation
            max_tokens=500
        )
        
        return response.choices[0].message.content.strip()
    
    def generate_query(self, question: str) -> str:
        """
        Generate a Cypher query from a natural language question.
        
        Args:
            question: Natural language question about the codebase
            
        Returns:
            Cypher query string or error message
        """
        return self._call_llm(question)
    
    def query(self, question: str, execute: bool = True) -> Dict[str, Any]:
        """
        Query the graph using natural language.
        
        Args:
            question: Natural language question
            execute: Whether to execute the query (if False, only returns the generated query)
            
        Returns:
            Dict with 'query', 'results' (if executed), and 'error' (if any)
        """
        result = {"question": question, "query": None, "results": None, "error": None}
        
        # Generate Cypher query
        cypher = self.generate_query(question)
        
        if cypher.startswith("ERROR:"):
            result["error"] = cypher
            return result
        
        result["query"] = cypher
        
        if not execute:
            return result
        
        # Execute the query
        try:
            results = self.graph.query(cypher)
            result["results"] = results
            
            # Update conversation history for context
            self.conversation_history.append({"role": "user", "content": question})
            self.conversation_history.append({
                "role": "assistant", 
                "content": f"Query: {cypher}\nResults: {len(results)} rows"
            })
            
            # Keep history manageable
            if len(self.conversation_history) > 10:
                self.conversation_history = self.conversation_history[-10:]
                
        except Exception as e:
            result["error"] = f"Query execution failed: {str(e)}"
        
        return result
    
    def clear_history(self):
        """Clear conversation history."""
        self.conversation_history = []
    
    def get_schema(self) -> str:
        """Return the graph schema documentation."""
        return GRAPH_SCHEMA