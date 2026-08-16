"""Agentic RAG engine — LangGraph-based retrieval-augmented generation.

Public interface:
- ``build_graph()`` → compiled LangGraph ``StateGraph``
- ``RAGService.run_rag_query(...)`` → typed ``RAGResponse``
"""

from __future__ import annotations
