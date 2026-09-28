"""Search engine for NØMAÐ reference knowledge base.

Provides full-text search with token matching and relevance scoring.
This is the Level 1 implementation — simple but effective. Level 2
adds semantic search via embeddings for the Console.
"""

from __future__ import annotations


from nomad.reference.knowledge_base import KnowledgeBase, ReferenceEntry


class SearchEngine:
    """Full-text search over the NØMAÐ knowledge base.

    Implements token-based search with TF-IDF-like scoring:
    - Exact key match: highest boost
    - Title match: high boost
    - Tag match: medium boost
    - Description/body match: base score
    """

    def __init__(self, kb: KnowledgeBase):
        self.kb = kb
        self._index: dict[str, list[str]] = {}  # token -> [entry keys]
        self._indexed = False

    def _ensure_indexed(self) -> None:
        """Build the inverted index on first search."""
        if self._indexed:
            return
        self._build_index()
        self._indexed = True

    def _build_index(self) -> None:
        """Build an inverted index from all entries."""
        self._index.clear()
        for entry in self.kb.list_topics():
            tokens = set(entry.searchable_text().split())
            for token in tokens:
                if token not in self._index:
                    self._index[token] = []
                self._index[token].append(entry.key)

    def search(
        self, query: str, max_results: int = 10
    ) -> list[tuple[ReferenceEntry, float]]:
        """Search with relevance scoring.

        Returns list of (entry, score) tuples, sorted by score descending.
        """
        self._ensure_indexed()
        tokens = query.lower().split()
        if not tokens:
            return []

        scores: dict[str, float] = {}

        for token in tokens:
            # Exact index matches
            if token in self._index:
                for key in self._index[token]:
                    scores[key] = scores.get(key, 0) + 1.0

            # Partial matches (prefix)
            for idx_token, keys in self._index.items():
                if idx_token.startswith(token) and idx_token != token:
                    for key in keys:
                        scores[key] = scores.get(key, 0) + 0.5

        # Apply boosts
        query_lower = query.lower()
        for key in scores:
            entry = self.kb.get(key)
            if entry is None:
                continue
            if query_lower in entry.key.lower():
                scores[key] += 5.0
            if query_lower in entry.title.lower():
                scores[key] += 3.0
            if any(query_lower in tag.lower() for tag in entry.tags):
                scores[key] += 2.0

        # Sort and return
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results = []
        for key, score in ranked[:max_results]:
            entry = self.kb.get(key)
            if entry:
                results.append((entry, score))

        return results
