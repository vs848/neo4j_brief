"""Lightweight keyword extraction using TF-IDF over per-competitor corpora."""
from __future__ import annotations

from typing import Iterable

from sklearn.feature_extraction.text import TfidfVectorizer

from .config import settings
from .models import KeywordScore


def top_keywords(
    documents: Iterable[str],
    top_k: int | None = None,
    ngram_range: tuple[int, int] = (1, 2),
) -> list[KeywordScore]:
    """
    Return the top-k TF-IDF terms (unigrams + bigrams) across ``documents``.

    Score is the summed TF-IDF weight across all input documents.
    """
    docs = [d for d in documents if d and d.strip()]
    if not docs:
        return []

    limit = top_k or settings.keywords_per_competitor

    vectoriser = TfidfVectorizer(
        stop_words="english",
        ngram_range=ngram_range,
        max_df=0.85,
        min_df=1,
        max_features=5000,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z-]{2,}\b",
    )
    try:
        matrix = vectoriser.fit_transform(docs)
    except ValueError:
        # e.g. empty vocabulary after stopword removal
        return []

    summed = matrix.sum(axis=0).A1  # type: ignore[attr-defined]
    terms = vectoriser.get_feature_names_out()

    scored = sorted(zip(terms, summed), key=lambda p: p[1], reverse=True)
    return [KeywordScore(term=term, score=float(score)) for term, score in scored[:limit] if score > 0]
