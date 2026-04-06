"""tofu_search.search.rerank — BM25-based reranking for search results."""

import math
import re
import time

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['rerank_by_bm25']

BM25_K1 = 1.5
BM25_B = 0.75
_MAX_RERANK_CHARS = 8_000

_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'who', 'new', 'get', 'set', 'one', 'two', 'any',
    'www', 'http', 'https', 'com', 'org', 'html',
})

_TOKENIZE_RE = re.compile(r'[^a-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+')
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]')


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens for BM25 scoring."""
    lowered = text.lower()
    lowered = lowered.replace('-', ' ').replace('_', ' ')
    raw_tokens = _TOKENIZE_RE.split(lowered)

    tokens = []
    for t in raw_tokens:
        if not t:
            continue
        cjk_chars = _CJK_RE.findall(t)
        if cjk_chars:
            cjk_str = ''.join(cjk_chars)
            for i in range(len(cjk_str) - 1):
                tokens.append(cjk_str[i:i+2])
            if len(cjk_str) <= 2:
                for c in cjk_str:
                    tokens.append(c)
            latin_part = _CJK_RE.sub(' ', t)
            for lt in latin_part.split():
                if lt and lt not in _STOP_WORDS and len(lt) > 1:
                    tokens.append(lt)
        elif t not in _STOP_WORDS and len(t) > 1:
            tokens.append(t)

    return tokens


def _build_doc_text(r: dict) -> str:
    """Build rankable text from a search result dict."""
    full = (r.get('full_content') or '').strip()
    title = (r.get('title') or '').strip()
    snippet = (r.get('snippet') or '').strip()

    if full:
        text = f'{title}\n\n{full}' if title else full
        return text[:_MAX_RERANK_CHARS]
    else:
        return f'{title} {snippet}' if title else snippet


def rerank_by_bm25(query: str, results: list[dict], top_k: int) -> list[dict]:
    """Rerank search results by BM25 score of query vs document content."""
    if not results:
        return []
    if len(results) <= top_k:
        return results

    t0 = time.time()

    query_tokens = _tokenize(query)
    if not query_tokens:
        logger.debug('[Rerank] No query tokens after tokenization, returning original order')
        return results[:top_k]

    query_terms = set(query_tokens)

    doc_texts = [_build_doc_text(r) for r in results]
    docs = [_tokenize(t) for t in doc_texts]
    doc_lens = [len(d) for d in docs]
    n = len(docs)
    avg_dl = sum(doc_lens) / n if n > 0 else 1.0

    df: dict[str, int] = {}
    for term in query_terms:
        df[term] = sum(1 for doc in docs if term in doc)

    scored = []
    for i, (result, doc, dl) in enumerate(zip(results, docs, doc_lens)):
        score = 0.0
        tf_map: dict[str, int] = {}
        for t in doc:
            if t in query_terms:
                tf_map[t] = tf_map.get(t, 0) + 1

        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            d = df.get(term, 0)
            idf = math.log((n - d + 0.5) / (d + 0.5) + 1.0)
            numerator = tf * (BM25_K1 + 1)
            denominator = tf + BM25_K1 * (1 - BM25_B + BM25_B * dl / avg_dl)
            score += idf * numerator / denominator

        scored.append((score, i, result))

    scored.sort(key=lambda x: (-x[0], x[1]))

    selected = [item[2] for item in scored[:top_k]]

    elapsed = time.time() - t0
    scores_str = ', '.join(f'#{item[1]}:{item[0]:.3f}' for item in scored[:top_k])
    logger.info('[Rerank] BM25 %d->%d results in %.1fms. Top-%d (idx:score): %s  query=%r',
                len(results), top_k, elapsed * 1000, top_k, scores_str, query[:60])

    return selected
