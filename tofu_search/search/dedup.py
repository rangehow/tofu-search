"""tofu_search.search.dedup — Content deduplication for search results."""

import re

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['dedup_by_content']

_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uac00-\ud7af\u3040-\u30ff]')


def _text_to_shingles(text: str) -> set[str]:
    """Convert text into a set of shingles (word tokens + CJK char bigrams)."""
    text = text.lower()
    tokens = set()

    cjk_chars = _CJK_RE.findall(text)
    if cjk_chars:
        for i in range(len(cjk_chars) - 1):
            tokens.add(cjk_chars[i] + cjk_chars[i + 1])
        if len(cjk_chars) < 6:
            tokens.update(cjk_chars)

    latin = _CJK_RE.sub(' ', text)
    latin = re.sub(r'[^\w\s]', ' ', latin)
    for w in latin.split():
        if len(w) > 1:
            tokens.add(w)

    return tokens


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_by_content(results: list[dict], threshold: float = 0.45) -> list[dict]:
    """Remove near-duplicate results based on title+snippet word overlap."""
    if len(results) <= 1:
        return results

    shingle_sets = []
    for r in results:
        title = (r.get('title') or '').strip()
        snippet = (r.get('snippet') or '').strip()
        shingle_sets.append(_text_to_shingles(f'{title} {snippet}'))

    keep = []
    keep_indices = []
    removed = 0
    for i, r in enumerate(results):
        is_dup = False
        for ki in keep_indices:
            sim = _jaccard(shingle_sets[i], shingle_sets[ki])
            if sim >= threshold:
                is_dup = True
                removed += 1
                break
        if not is_dup:
            keep.append(r)
            keep_indices.append(i)

    if removed:
        logger.info('[ContentDedup] %d->%d results (removed %d near-duplicates, threshold=%.2f)',
                    len(results), len(keep), removed, threshold)
    return keep
