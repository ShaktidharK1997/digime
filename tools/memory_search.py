"""Lightweight BM25 relevance ranking over memory gists.

Pure-Python, dependency-free. At the scale of this bot (hundreds of
one-sentence gists) BM25 is plenty — no embedding API or vector store needed.
Used to pick which past-conversation gists get injected into the system
prompt, instead of blindly taking the N most recent.
"""

import math
import re

_TOKEN_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "had", "has", "have", "he", "her", "his", "i", "in", "is", "it", "its",
    "me", "my", "of", "on", "or", "s", "t", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "what", "which", "with", "you",
    "your",
})

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def rank(query: str, documents: list[str], top_k: int = 8) -> list[int]:
    """Rank documents by BM25 relevance to the query.

    Args:
        query: The search text (typically the user's current message).
        documents: Corpus to score (gist strings).
        top_k: Maximum number of results.

    Returns:
        Indices into `documents`, best match first. Only documents with a
        positive score are returned, so the list may be shorter than top_k
        (or empty when nothing overlaps).
    """
    query_terms = _tokenize(query)
    if not query_terms or not documents:
        return []

    doc_tokens = [_tokenize(doc) for doc in documents]
    doc_lengths = [len(tokens) for tokens in doc_tokens]
    avg_length = sum(doc_lengths) / len(doc_lengths) if doc_lengths else 0
    if avg_length == 0:
        return []

    n_docs = len(documents)

    # Document frequency per query term
    doc_freq: dict[str, int] = {}
    for term in set(query_terms):
        doc_freq[term] = sum(1 for tokens in doc_tokens if term in tokens)

    scores: list[float] = []
    for tokens, length in zip(doc_tokens, doc_lengths):
        score = 0.0
        if length:
            counts: dict[str, int] = {}
            for t in tokens:
                counts[t] = counts.get(t, 0) + 1
            for term in query_terms:
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                df = doc_freq.get(term, 0)
                idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
                score += idf * (tf * (_K1 + 1)) / (
                    tf + _K1 * (1 - _B + _B * length / avg_length)
                )
        scores.append(score)

    ranked = sorted(range(n_docs), key=lambda i: scores[i], reverse=True)
    return [i for i in ranked if scores[i] > 0][:top_k]
