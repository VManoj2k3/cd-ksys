"""Phase 2 RAG: per-user guideline collections + retrieval-augmented review.

Fully on-prem: local embeddings, local disk-backed vector store, no cloud.
Additive to Phase 1 — the guideline layer only runs when the user turns the
RAG toggle on and selects a collection.
"""
