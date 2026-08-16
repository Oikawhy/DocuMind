"""Graph node functions for the RAG agent per §7.2/§7.4.

Each node is a function that takes ``AgentState`` and returns a partial
state update dict.  Nodes are assembled into a LangGraph StateGraph
in ``graph.py``.
"""

from __future__ import annotations
