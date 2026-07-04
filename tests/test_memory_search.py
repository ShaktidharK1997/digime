"""Tests for the BM25 gist ranker."""

from tools.memory_search import rank

DOCS = [
    "Checked Gmail for emails from manager about the project deadline",
    "Created TickTick task to buy groceries at Trader Joes",
    "Searched web for a biryani recipe and checked dietary profile for restrictions",
    "Reviewed overdue tasks and rescheduled two to next week",
]


def test_relevant_doc_ranks_first():
    result = rank("that biryani recipe we found", DOCS)
    assert result[0] == 2


def test_multiple_matches_ordered_by_relevance():
    result = rank("emails from my manager about deadline", DOCS)
    assert result[0] == 0


def test_no_overlap_returns_empty():
    assert rank("quantum chromodynamics", DOCS) == []


def test_empty_query_returns_empty():
    assert rank("", DOCS) == []
    assert rank("the and of", DOCS) == []  # stopwords only


def test_empty_corpus_returns_empty():
    assert rank("anything", []) == []


def test_top_k_limits_results():
    docs = [f"task number {i} about groceries" for i in range(10)]
    assert len(rank("groceries task", docs, top_k=3)) == 3
