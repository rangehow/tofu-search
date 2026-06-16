"""tofu_search.search.orchestrator — Parallel multi-engine search pipeline.

Pipeline order (cheap → expensive):
  1+4 MERGED: engines fire in parallel; as each engine returns results,
      URLs are immediately deduped and submitted to the fetch pool. Page
      fetching starts as soon as the FIRST engine responds, overlapping
      with slower engines.
  2. URL dedup — runs incrementally as each engine batch arrives.
  3. Content dedup (Jaccard on title+snippet shingles) — once after engines.
  5. LLM content filter — relevance verdict + noise removal (if LLM configured).
  6. BM25 rerank — on cleaned full text → top-N (pure Python, no API call).
"""
# HOT_PATH

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from tofu_search.config import get_config
from tofu_search.fetch import fetch_page_content
from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL, filter_web_contents_batch
from tofu_search.log import get_logger
from tofu_search.search.browser_fallback import search_via_browser
from tofu_search.search.dedup import dedup_by_content
from tofu_search.search.deepen import deepen_results, is_deepen_enabled
from tofu_search.search.engines.bing import search_bing
from tofu_search.search.engines.brave import search_brave
from tofu_search.search.engines.ddg import search_ddg_api, search_ddg_html
from tofu_search.search.engines.marginalia import search_marginalia
from tofu_search.search.engines.searxng import search_searxng
from tofu_search.search.engines.xhs import search_xhs, xhs_search_available
from tofu_search.search.rerank import rerank_by_bm25

logger = get_logger(__name__)

__all__ = ['perform_web_search', 'SearchResultList']


class SearchResultList(list):
    """A list subclass that carries diagnostic metadata.

    ``_search_diag`` is set when 0 results (reason/detail/engine state).
    ``_engine_breakdown`` maps engine tag → [{url, title}] for raw results.
    """
    _search_diag = None
    _engine_breakdown = None


def _url_dedup_key(url: str) -> str:
    """Normalise a URL into a dedup key (strip only the leading scheme)."""
    key = url.lower().rstrip('/')
    for scheme in ('https://', 'http://'):
        if key.startswith(scheme):
            key = key[len(scheme):]
            break
    return key[:150]


def perform_web_search(query, max_results=None, user_question='', freshness='',
                       *, fetch_pages=True, filter_pages=True, rerank=True,
                       engines=None, max_chars_per_page=None, deepen=None,
                       config=None):
    """Run search engines and page fetches in an overlapping streaming pipeline.

    Args:
        query: Search query string.
        max_results: Max results to return. Defaults to config.fetch_top_n.
        user_question: The user's original question (true intent).
        freshness: Time filter — 'day', 'week', 'month', 'year', or '' (none).
        fetch_pages: When False, skip page fetch and return engine snippets.
        filter_pages: When False, skip the LLM relevance filter.
        rerank: When False, skip BM25 rerank.
        engines: Optional iterable of engine tags (subset). None ⇒ all.
        max_chars_per_page: Override fetch_max_chars_search for this call.
        deepen: When True, follow top query-relevant links one hop deeper.
                None ⇒ SEARCH_DEEPEN_HOPS env default (off).
        config: Optional SearchConfig override.

    Returns:
        SearchResultList: Search results with diagnostics.
    """
    if config is None:
        config = get_config()

    pipeline_t0 = time.time()
    step_timings = {}

    if max_results is None:
        max_results = config.fetch_top_n

    if freshness and freshness not in ('day', 'week', 'month', 'year'):
        logger.warning('[Search] Ignoring unknown freshness=%r (expected day|week|month|year)', freshness)
        freshness = ''

    _lock = threading.Lock()
    seen_urls: set[str] = set()
    all_results: list[dict] = []
    unique_results: list[dict] = []
    fetch_futs: dict[Future, dict] = {}
    url_timings: list[tuple] = []

    target_ok = config.fetch_top_n * 2

    engine_counts = {}
    engine_timings = {}
    engine_errors = {}
    engine_empty = []

    ALL_ENGINE_NAMES = ['DDG-HTML', 'Brave', 'Bing', 'DDG-API', 'SearXNG', 'Marginalia']
    _xhs_on = xhs_search_available()
    if _xhs_on:
        ALL_ENGINE_NAMES.append('Xiaohongshu')
    if engines:
        engine_allow = {e for e in engines if e in ALL_ENGINE_NAMES}
        if not engine_allow:
            logger.warning('[Search] all requested engines unknown (%s) — falling back to default set',
                           list(engines))
            engine_allow = set(ALL_ENGINE_NAMES)
    else:
        engine_allow = set(ALL_ENGINE_NAMES)

    max_chars = max_chars_per_page if max_chars_per_page else config.fetch_max_chars_search
    pdf_max_chars = config.fetch_max_chars_pdf

    fetch_pool = ThreadPoolExecutor(max_workers=16)
    first_fetch_submitted_at = None

    def _submit_fetches_for_batch(batch: list[dict]):
        """Dedup a batch of engine results and submit new URLs to fetch pool."""
        nonlocal first_fetch_submitted_at
        new_results = []
        with _lock:
            for r in batch:
                key = _url_dedup_key(r['url'])
                if key not in seen_urls:
                    seen_urls.add(key)
                    unique_results.append(r)
                    new_results.append(r)
            all_results.extend(batch)

        if not new_results or not fetch_pages:
            return

        def _do_fetch(result_dict):
            url = result_dict['url']
            t0 = time.time()
            content = fetch_page_content(url, max_chars=max_chars, pdf_max_chars=pdf_max_chars)
            elapsed = time.time() - t0
            return result_dict, content, elapsed

        with _lock:
            for r in new_results:
                fut = fetch_pool.submit(_do_fetch, r)
                fetch_futs[fut] = r
            if first_fetch_submitted_at is None:
                first_fetch_submitted_at = time.time()
                logger.info('[Search] ⚡ First fetch submitted at +%.1fs (pipeline overlap started)',
                            first_fetch_submitted_at - pipeline_t0)

    # ══ Step 1: Fire all engines + immediate fetch submission ══
    step1_t0 = time.time()

    _ENGINE_SPECS = [
        ('DDG-HTML', search_ddg_html, 20),
        ('Brave',    search_brave,    20),
        ('Bing',     search_bing,     20),
        ('DDG-API',  search_ddg_api,   6),
        ('SearXNG',  search_searxng,   6),
        ('Marginalia', search_marginalia, 6),
    ]
    if _xhs_on and 'Xiaohongshu' in engine_allow:
        _ENGINE_SPECS.append(('Xiaohongshu', search_xhs, 10))
    with ThreadPoolExecutor(max_workers=max(1, len(engine_allow))) as engine_pool:
        engine_futs = {
            engine_pool.submit(fn, query, n, freshness): tag
            for tag, fn, n in _ENGINE_SPECS if tag in engine_allow
        }
        try:
            for fut in as_completed(engine_futs, timeout=20):
                tag = engine_futs[fut]
                engine_elapsed = time.time() - step1_t0
                try:
                    r = fut.result()
                    if r:
                        engine_counts[tag] = len(r)
                        engine_timings[tag] = engine_elapsed
                        logger.info('[Search] ✓ %s returned %d results in %.1fs → submitting fetches',
                                    tag, len(r), engine_elapsed)
                        _submit_fetches_for_batch(r)
                    else:
                        engine_empty.append(tag)
                        engine_timings[tag] = engine_elapsed
                        logger.info('[Search] ○ %s returned 0 results in %.1fs', tag, engine_elapsed)
                except Exception as e:
                    logger.warning('[Search] ✗ %s failed in %.1fs: %s', tag, engine_elapsed, e)
                    engine_errors[tag] = str(e)[:200]
                    engine_timings[tag] = engine_elapsed
        except TimeoutError:
            timed_out = [engine_futs[f] for f in engine_futs if not f.done()]
            for name in timed_out:
                engine_errors[name] = 'Timed out after 20s'
                engine_timings[name] = 20.0
            logger.warning('[Search] %d/%d engines timed out (%s), keeping %d results from others. query=%r',
                           len(timed_out), len(engine_futs), ', '.join(timed_out),
                           len(all_results), query[:80])

    step_timings['step1_engines'] = time.time() - step1_t0

    if engine_counts:
        logger.info('[Search] Engine results: %s  timings: %s  (query=%r)',
                    ', '.join(f'{k}={v}' for k, v in engine_counts.items()),
                    ', '.join(f'{k}={v:.1f}s' for k, v in sorted(engine_timings.items(), key=lambda x: x[1])),
                    query[:60])

    # ── Retry: if we got nothing, give DDG+Brave another chance ──
    if not all_results:
        logger.info('[Search] 0 results on first attempt, retrying DDG+Brave after 0.8s for query=%r', query[:80])
        time.sleep(0.8)
        retry = search_ddg_html(query, max_results)
        if retry:
            _submit_fetches_for_batch(retry)
        else:
            retry_brave = search_brave(query, max_results)
            if retry_brave:
                _submit_fetches_for_batch(retry_brave)

    # ── Browser fallback: server network may be down but user browser works ──
    if not all_results:
        browser_results = search_via_browser(query, max_results)
        if browser_results:
            logger.info('[Search] Browser fallback produced %d results for query=%r',
                        len(browser_results), query[:80])
            _submit_fetches_for_batch(browser_results)

    # ── Build engine breakdown for diagnostics (before dedup) ──
    engine_breakdown = {}
    for r in all_results:
        eng = r.get('source', 'Unknown')
        engine_breakdown.setdefault(eng, []).append({
            'url': r['url'],
            'title': r.get('title', '')[:100],
        })

    url_dedup_count = len(unique_results)
    step_timings['step2_url_dedup'] = 0.0

    # ── Step 3: Content dedup on the unique results ──
    step3_t0 = time.time()
    if len(unique_results) > max_results:
        unique_results = dedup_by_content(unique_results)
    content_dedup_count = len(unique_results)
    step_timings['step3_content_dedup'] = time.time() - step3_t0

    kept_urls = {r['url'] for r in unique_results}

    # ── Dynamically reduce target_ok when candidate pool is too small ──
    _original_target_ok = target_ok
    if len(kept_urls) < target_ok * 1.5:
        target_ok = max(max_results, int(len(kept_urls) / 1.5))
        if target_ok < _original_target_ok:
            logger.info('[Fetch] target_ok reduced %d → %d '
                        '(candidate pool=%d, need headroom for Race-to-N)',
                        _original_target_ok, target_ok, len(kept_urls))

    # ══ Step 4: Wait for fetch futures (already running) ══
    step4_t0 = time.time()
    with _lock:
        pending_futs = set(fetch_futs.keys())

    if pending_futs:
        logger.info('[Fetch] Waiting for %d in-flight fetches (started %.1fs ago), target_ok=%d',
                    len(pending_futs), time.time() - (first_fetch_submitted_at or pipeline_t0),
                    target_ok)
        # Count of KEPT (post-content-dedup) pages that came back with content.
        # Maintained incrementally as each future completes — the previous
        # implementation re-scanned all of unique_results on every completion
        # (O(n²)) which both wasted work and was easy to get wrong.
        kept_ok = 0
        try:
            for fut in as_completed(pending_futs, timeout=90):
                try:
                    result_dict, content, fetch_elapsed = fut.result()
                    url = result_dict['url']
                    ok = bool(content and len(content) > 50)
                    chars = len(content) if content else 0
                    url_timings.append((url, fetch_elapsed, ok, chars))
                    if ok:
                        result_dict['full_content'] = content
                        if url in kept_urls:
                            kept_ok += 1
                    if fetch_elapsed > 5:
                        logger.info('[Fetch] ⚠ SLOW url=%.80s  %.1fs  ok=%s chars=%d',
                                    url, fetch_elapsed, ok, chars)
                except Exception as e:
                    logger.warning('[Fetch] fetch thread error: %s', e, exc_info=True)

                if kept_ok >= target_ok:
                    remaining = [f for f in pending_futs if not f.done()]
                    if remaining:
                        elapsed_so_far = time.time() - (first_fetch_submitted_at or step4_t0)
                        logger.info('[Fetch] Race-to-N: got %d/%d pages in %.1fs, '
                                    'cancelling %d slow fetches',
                                    kept_ok, len(pending_futs), elapsed_so_far, len(remaining))
                        for f in remaining:
                            f.cancel()
                        break
        except TimeoutError:
            logger.warning('[Fetch] as_completed timeout (90s)', exc_info=True)

    fetch_pool.shutdown(wait=True, cancel_futures=True)

    fetch_count = sum(1 for r in unique_results if r.get('full_content'))
    step_timings['step4_page_fetch'] = time.time() - step4_t0

    # ── Step 4b: One-hop link-following (depth) — opt-in ──
    _do_deepen = is_deepen_enabled() if deepen is None else deepen
    if _do_deepen and fetch_pages and fetch_count:
        step4b_t0 = time.time()
        try:
            deeper = deepen_results(query, unique_results,
                                    max_chars=max_chars, pdf_max_chars=pdf_max_chars)
        except Exception as e:
            logger.error('[Search] deepen stage failed: %s', e, exc_info=True)
            deeper = []
        for dr in deeper:
            key = _url_dedup_key(dr['url'])
            if key not in seen_urls:
                seen_urls.add(key)
                unique_results.append(dr)
                kept_urls.add(dr['url'])
        step_timings['step4b_deepen'] = time.time() - step4b_t0
        if deeper:
            logger.info('[Search] Deepen added %d pages in %.1fs',
                        len(deeper), step_timings['step4b_deepen'])

    if first_fetch_submitted_at:
        overlap_duration = step_timings['step1_engines'] - (first_fetch_submitted_at - pipeline_t0)
        if overlap_duration > 0.5:
            logger.info('[Search] ⚡ Pipeline overlap saved ~%.1fs '
                        '(fetches started at +%.1fs, engines finished at +%.1fs)',
                        overlap_duration,
                        first_fetch_submitted_at - pipeline_t0,
                        step_timings['step1_engines'])

    # ── Step 5: LLM content filter — relevance + cleaning ──
    step5_t0 = time.time()
    irrelevant_urls: set[str] = set()
    _filter_on = config.filter_enabled and config.has_llm()
    if not filter_pages:
        logger.debug('[Search] step5 skipped — caller passed filter_pages=False')
    elif not _filter_on:
        logger.debug('[Search] step5 skipped — filter disabled or no LLM configured')
    else:
        to_filter = [(r['url'], r['full_content']) for r in unique_results
                     if r.get('full_content')]
        if to_filter:
            logger.info('[Search] LLM-filtering %d/%d fetched pages, query=%r user_question=%r',
                        len(to_filter), len(unique_results), query[:80], user_question[:80])
            filtered = filter_web_contents_batch(to_filter, query=query,
                                                 user_question=user_question,
                                                 min_chars=0, config=config)
            for r in unique_results:
                if r['url'] in filtered:
                    val = filtered[r['url']]
                    if val == IRRELEVANT_SENTINEL:
                        irrelevant_urls.add(r['url'])
                        r['full_content'] = ''
                        logger.info('[Search] ✗ IRRELEVANT dropped: %s', r['url'][:100])
                    else:
                        r['full_content'] = val
            if irrelevant_urls:
                logger.info('[Search] Dropped %d/%d irrelevant pages',
                            len(irrelevant_urls), len(to_filter))

    step_timings['step5_llm_filter'] = time.time() - step5_t0

    relevant = [r for r in unique_results if r['url'] not in irrelevant_urls]

    # ── Step 5b: Deprioritize results without full content ──
    has_content = [r for r in relevant if r.get('full_content')]
    no_content  = [r for r in relevant if not r.get('full_content')]
    relevant = has_content + no_content

    # ── Step 6: BM25 rerank on cleaned full text → top-N ──
    step6_t0 = time.time()
    if not rerank:
        logger.debug('[Search] step6 skipped — caller passed rerank=False')
    elif len(has_content) > max_results:
        relevant = rerank_by_bm25(query, has_content, max_results)
    elif len(relevant) > max_results:
        relevant = rerank_by_bm25(query, relevant, max_results)
    final_count = min(len(relevant), max_results)
    step_timings['step6_bm25_rerank'] = time.time() - step6_t0

    pipeline_total = time.time() - pipeline_t0
    step_timings['total'] = pipeline_total

    timing_parts = []
    for step_name in ['step1_engines', 'step2_url_dedup', 'step3_content_dedup',
                      'step4_page_fetch', 'step5_llm_filter', 'step6_bm25_rerank']:
        elapsed = step_timings.get(step_name, 0)
        timing_parts.append(f'{step_name}={elapsed:.1f}s')
    timing_str = ', '.join(timing_parts)

    logger.info('[Search] Pipeline: %d raw → %d url-dedup → %d content-dedup → '
                '%d fetched → -%d irrelevant → %d relevant → %d reranked  '
                'TOTAL=%.1fs  [%s]  query=%r',
                len(all_results), url_dedup_count, content_dedup_count,
                fetch_count, len(irrelevant_urls), len(relevant),
                final_count, pipeline_total, timing_str, query[:60])

    if url_timings:
        url_timings.sort(key=lambda x: -x[1])
        slow_summary = '  '.join(
            f'[{"✓" if ok else "✗"}]{url[:50]}={et:.1f}s'
            for url, et, ok, _chars in url_timings[:8]
        )
        logger.info('[Fetch] Timing breakdown (slowest first): %s', slow_summary)

    if step_timings.get('step4_page_fetch', 0) > 15:
        logger.warning('[Search] ⚠ SLOW step4_page_fetch=%.1fs (>15s threshold). query=%r',
                       step_timings['step4_page_fetch'], query[:60])
    if step_timings.get('step5_llm_filter', 0) > 20:
        logger.warning('[Search] ⚠ SLOW step5_llm_filter=%.1fs (>20s threshold). query=%r',
                       step_timings['step5_llm_filter'], query[:60])
    if pipeline_total > 30:
        logger.warning('[Search] ⚠ SLOW PIPELINE total=%.1fs (>30s threshold) — breakdown: %s  query=%r',
                       pipeline_total, timing_str, query[:60])

    final_results = SearchResultList(relevant[:max_results])
    final_results._engine_breakdown = engine_breakdown

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
            reason_detail = (
                'All search engines responded but found no matching results for this query.'
            )
        diag = {
            'reason': reason,
            'reason_detail': reason_detail,
            'engine_errors': engine_errors,
            'engine_empty': engine_empty,
            'engine_ok': list(engine_counts.keys()),
        }
        final_results._search_diag = diag
        logger.warning('[Search] 0 final results — diag: reason=%s errors=%s empty=%s query=%r',
                       reason, list(engine_errors.keys()), engine_empty, query[:80])

    return final_results
