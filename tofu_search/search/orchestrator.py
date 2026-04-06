"""tofu_search.search.orchestrator — Parallel multi-engine search pipeline.

Pipeline order:
  1. 5 engines in parallel (DDG x2 + Brave + Bing + SearXNG)
  2. URL dedup
  3. Content dedup (Jaccard on title+snippet shingles)
  4. Page fetch — "race to N" concurrent fetch
  5. LLM content filter — relevance verdict + noise removal (if LLM configured)
  6. BM25 rerank — on cleaned full text -> top-N
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tofu_search.config import get_config
from tofu_search.fetch import fetch_contents_for_results
from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_contents_batch
from tofu_search.log import get_logger
from tofu_search.search.dedup import dedup_by_content
from tofu_search.search.engines.bing import search_bing
from tofu_search.search.engines.brave import search_brave
from tofu_search.search.engines.ddg import search_ddg_api, search_ddg_html
from tofu_search.search.engines.searxng import search_searxng
from tofu_search.search.rerank import rerank_by_bm25

logger = get_logger(__name__)

__all__ = ['perform_web_search']


class SearchResultList(list):
    """A list subclass that can carry diagnostic metadata."""
    _search_diag = None


def perform_web_search(query, max_results=None, user_question='', config=None):
    """Run all search engines in parallel, then progressively narrow results.

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to config.fetch_top_n.
        user_question: The user's original question. Used by the LLM content
                       filter to judge relevance.
        config: Optional SearchConfig override.

    Returns:
        SearchResultList: List of search result dicts. When empty, a
        ``_search_diag`` attribute provides diagnostics.
    """
    if config is None:
        config = get_config()
    if max_results is None:
        max_results = config.fetch_top_n

    all_results = []
    engine_counts = {}
    engine_errors = {}
    engine_empty = []

    ALL_ENGINE_NAMES = ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG']

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {
            pool.submit(search_ddg_html, query, 20): 'DDG-HTML',
            pool.submit(search_brave, query, 20):     'Brave',
            pool.submit(search_bing, query, 20):       'Bing',
            pool.submit(search_ddg_api, query, 6):    'DDG-API',
            pool.submit(search_searxng, query, 6):    'SearXNG',
        }
        try:
            for fut in as_completed(futs, timeout=20):
                tag = futs[fut]
                try:
                    r = fut.result()
                    if r:
                        all_results.extend(r)
                        engine_counts[tag] = len(r)
                    else:
                        engine_empty.append(tag)
                except Exception as e:
                    logger.warning('[Search] %s failed: %s', tag, e)
                    engine_errors[tag] = str(e)[:200]
        except TimeoutError:
            timed_out = [futs[f] for f in futs if not f.done()]
            for name in timed_out:
                engine_errors[name] = 'Timed out after 20s'
            logger.warning('[Search] %d/%d engines timed out (%s), keeping %d results',
                           len(timed_out), len(futs), ', '.join(timed_out), len(all_results))

    if engine_counts:
        logger.info('[Search] Engine results: %s (query=%r)',
                    ', '.join(f'{k}={v}' for k, v in engine_counts.items()), query[:60])

    # Retry if nothing found
    if not all_results:
        logger.info('[Search] 0 results on first attempt, retrying DDG+Brave for query=%r', query[:80])
        time.sleep(0.8)
        retry = search_ddg_html(query, max_results)
        if retry:
            all_results.extend(retry)
        else:
            retry_brave = search_brave(query, max_results)
            if retry_brave:
                all_results.extend(retry_brave)

    # Step 2: URL dedup
    seen, unique = set(), []
    for r in all_results:
        key = r['url'].lower().rstrip('/').replace('https://', '').replace('http://', '')[:150]
        if key not in seen:
            seen.add(key)
            unique.append(r)

    url_dedup_count = len(unique)

    # Step 3: Content dedup
    if len(unique) > max_results:
        unique = dedup_by_content(unique)
    content_dedup_count = len(unique)

    # Step 4: Page fetch
    unique = fetch_contents_for_results(unique, max_fetch=len(unique))
    fetch_count = sum(1 for r in unique if r.get('full_content'))

    # Step 5: LLM content filter (skipped if no LLM configured)
    to_filter = [(r['url'], r['full_content']) for r in unique
                 if r.get('full_content')]
    irrelevant_urls: set[str] = set()
    if to_filter:
        logger.info('[Search] LLM-filtering %d/%d fetched pages, query=%r',
                    len(to_filter), len(unique), query[:80])
        filtered = filter_web_contents_batch(to_filter, query=query,
                                             user_question=user_question,
                                             min_chars=0, config=config)
        for r in unique:
            if r['url'] in filtered:
                val = filtered[r['url']]
                if val == IRRELEVANT_SENTINEL:
                    irrelevant_urls.add(r['url'])
                    r['full_content'] = ''
                    logger.info('[Search] IRRELEVANT dropped: %s', r['url'][:100])
                else:
                    r['full_content'] = val
        if irrelevant_urls:
            logger.info('[Search] Dropped %d/%d irrelevant pages',
                        len(irrelevant_urls), len(to_filter))

    # Remove irrelevant results
    relevant = [r for r in unique if r['url'] not in irrelevant_urls]

    # Deprioritize results without content
    has_content = [r for r in relevant if r.get('full_content')]
    no_content  = [r for r in relevant if not r.get('full_content')]
    relevant = has_content + no_content

    # Step 6: BM25 rerank
    if len(has_content) > max_results:
        relevant = rerank_by_bm25(query, has_content, max_results)
    elif len(relevant) > max_results:
        relevant = rerank_by_bm25(query, relevant, max_results)
    final_count = min(len(relevant), max_results)

    logger.info('[Search] Pipeline: %d raw -> %d url-dedup -> %d content-dedup -> '
                '%d fetched -> -%d irrelevant -> %d relevant -> %d reranked  query=%r',
                len(all_results), url_dedup_count, content_dedup_count,
                fetch_count, len(irrelevant_urls), len(relevant),
                final_count, query[:60])

    final_results = SearchResultList(relevant[:max_results])

    # Diagnostics when 0 results
    if not final_results:
        total_engines = len(ALL_ENGINE_NAMES)
        errored = len(engine_errors)
        empty = len(engine_empty)
        if errored == total_engines:
            reason = 'network_error'
            reason_detail = 'All %d search engines failed due to network errors.' % total_engines
        elif errored > 0 and errored >= empty:
            reason = 'partial_network_error'
            failed_names = ', '.join(sorted(engine_errors.keys()))
            reason_detail = (
                '%d/%d engines had network errors (%s); the rest returned no matches.'
                % (errored, total_engines, failed_names)
            )
        else:
            reason = 'no_matches'
            reason_detail = 'All search engines responded but found no matching results.'
        diag = {
            'reason': reason,
            'reason_detail': reason_detail,
            'engine_errors': engine_errors,
            'engine_empty': engine_empty,
            'engine_ok': list(engine_counts.keys()),
        }
        final_results._search_diag = diag
        logger.warning('[Search] 0 results — diag: reason=%s errors=%s query=%r',
                       reason, list(engine_errors.keys()), query[:80])

    return final_results
