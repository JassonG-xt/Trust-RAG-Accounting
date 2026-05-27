"""Service layer.

Services encapsulate side-effectful or data-bearing collaborators (knowledge
bases, vector stores, LLM clients, ...). Keeping them out of graph nodes
makes node logic easy to test and lets us swap mocks for real backends.
"""
