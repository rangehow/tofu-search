"""tofu_search.fetch.content_filter — LLM-based web content relevance filtering.

Standalone version — uses tofu_search.llm_adapter instead of chatui's dispatch_chat.
When no LLM is configured, filter is silently skipped (raw text returned as-is).
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tofu_search.config import get_config
from tofu_search.log import get_logger

logger = get_logger(__name__)

_IRRELEVANT_STOP = '\u00a7\u00a7IRRELEVANT\u00a7\u00a7'
IRRELEVANT_SENTINEL = '[IRRELEVANT]'

_SYSTEM_PROMPT = """\
You are a web page content cleaner. Your ONLY job is to reformat raw extracted text \
into clean, readable content and remove obvious junk. You must NOT interpret, \
summarize, or answer any question -- just clean the text.

You will be given context about the user's intent (when available). Use it ONLY to \
judge relevance in Step 1 -- do NOT let it influence what content you keep or remove \
in Step 2.

## Step 1 -- Relevance verdict (MANDATORY first line)

Output exactly ONE of these two tokens on the FIRST line:

- `[USEFUL]` -- if the page contains substantive content (articles, docs, code, \
discussions, data, etc.).
- `\u00a7\u00a7IRRELEVANT\u00a7\u00a7` -- if the page does NOT help answer the user's question. This includes: \
empty/broken pages (login wall, captcha, cookie wall, 404, access denied, blank page), \
AND pages whose content is entirely unrelated to or does not help answer the user's question. \
If the page contains ANY information that could help -- even partially or indirectly -- output [USEFUL]. \
Generation stops immediately after this token.

When in doubt, output [USEFUL]. Err on the side of keeping content.

## Step 2 -- Content cleaning (only after [USEFUL])

**Your job: format optimization + junk removal. Keep everything else INTACT.**

**KEEP (preserve original wording, do not paraphrase or summarize):**
- ALL substantive text: articles, paragraphs, explanations, opinions, arguments
- ALL technical content: code, APIs, configs, commands, formulas, version strings
- ALL data: numbers, dates, names, URLs, tables, statistics, quotes
- ALL discussion content: questions, answers, comments with substance
- Document structure: headings, lists, sections -- improve formatting if messy

**REMOVE (only these categories of junk):**
- Navigation menus, breadcrumbs, site headers/footers, sidebars
- Ads, promotions, "related articles", "you might also like", "trending now"
- Cookie/login/newsletter banners and popups
- Social sharing buttons, "follow us", "share this"
- Legal boilerplate (privacy policy links, copyright footers)
- Duplicate/repeated text blocks
- Pagination chrome ("page 1 of 5", "next", "load more")

**NEVER do any of these:**
- Do NOT summarize or condense the content
- Do NOT answer questions based on the content
- Do NOT remove substantive content that relates to the user's question
- Do NOT add your own commentary or analysis
- Do NOT rewrite or paraphrase the author's words

Output the cleaned content directly after [USEFUL] -- no preamble, no wrapper."""


def filter_web_content(raw_text: str, *, url: str = '', query: str = '',
                       user_question: str = '',
                       timeout: int | None = None,
                       min_chars: int | None = None,
                       config=None) -> str:
    """Filter noise from web page text using LLM.

    Args:
        raw_text: Raw extracted text from web page.
        url: Source URL (for context).
        query: Search query used to find this page.
        user_question: The user's original question (true intent).
        timeout: Override timeout in seconds.
        min_chars: Override minimum character threshold. Set to 0 to
                   force all documents through the LLM filter.
        config: Optional SearchConfig override.

    Returns:
        Filtered text, IRRELEVANT_SENTINEL if page is irrelevant,
        or original raw_text if filtering fails/is disabled/no LLM configured.
    """
    if config is None:
        config = get_config()

    effective_min = config.filter_min_chars if min_chars is None else min_chars

    if not config.filter_enabled:
        logger.debug('[ContentFilter] SKIP (disabled) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    # No LLM configured — gracefully skip
    if not config.has_llm():
        logger.debug('[ContentFilter] SKIP (no LLM configured) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    # PDF content is already structured — skip
    if url and url.lower().rstrip('/').endswith('.pdf'):
        logger.debug('[ContentFilter] SKIP (PDF) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    if len(raw_text) < effective_min:
        logger.debug('[ContentFilter] SKIP (too short: %d < %d) url=%s',
                     len(raw_text), effective_min, url[:80])
        return raw_text

    from tofu_search.llm_adapter import call_llm

    _timeout = timeout or config.filter_timeout
    logger.info('[ContentFilter] START url=%s raw_chars=%d query=%r timeout=%ds',
                url[:100], len(raw_text), query[:80] if query else '', _timeout)

    user_parts = []
    if user_question:
        user_parts.append(f"User's original question: {user_question}")
    if query:
        user_parts.append(f"Search query: {query}")
    if url:
        user_parts.append(f"Source URL: {url}")
    user_parts.append(f"\n--- Raw page content ({len(raw_text):,} chars) ---\n{raw_text}")

    messages = [
        {'role': 'system', 'content': _SYSTEM_PROMPT},
        {'role': 'user', 'content': '\n'.join(user_parts)},
    ]

    t0 = time.time()
    try:
        content = call_llm(
            messages,
            config=config,
            temperature=0,
            stop=[_IRRELEVANT_STOP],
            timeout=_timeout,
        )

        elapsed = time.time() - t0

        _stripped = (content or '').strip()
        if (not _stripped
                or _stripped == _IRRELEVANT_STOP
                or _stripped.startswith(IRRELEVANT_SENTINEL)
                or _stripped.startswith(_IRRELEVANT_STOP)):
            logger.info('[ContentFilter] IRRELEVANT url=%s query=%r %.1fs',
                        url[:100], query[:60] if query else '', elapsed)
            return IRRELEVANT_SENTINEL

        if _stripped.startswith('[USEFUL]'):
            content = _stripped[len('[USEFUL]'):].lstrip('\n')

        if content and len(content) > 100:
            reduction = (1 - len(content) / len(raw_text)) * 100
            logger.info('[ContentFilter] DONE url=%s %s -> %s chars (%.0f%% reduction) %.1fs',
                        url[:100], f'{len(raw_text):,}', f'{len(content):,}',
                        reduction, elapsed)
            return content
        else:
            logger.warning('[ContentFilter] FAIL — LLM returned too-short (%d chars), using raw  url=%s',
                           len(content) if content else 0, url[:100])
            return raw_text

    except Exception as e:
        elapsed = time.time() - t0
        logger.error('[ContentFilter] ERROR after %.1fs: %s  url=%s',
                     elapsed, str(e)[:300], url[:100], exc_info=True)
        return raw_text


def filter_web_contents_batch(items: list[tuple[str, str]], *,
                              query: str = '',
                              user_question: str = '',
                              timeout: int | None = None,
                              min_chars: int | None = None,
                              config=None) -> dict[str, str]:
    """Filter multiple web pages in parallel."""
    if config is None:
        config = get_config()

    effective_min = config.filter_min_chars if min_chars is None else min_chars

    if not config.filter_enabled or not config.has_llm():
        return {url: text for url, text in items}

    results = {}
    to_filter = []
    for url, text in items:
        if url and url.lower().rstrip('/').endswith('.pdf'):
            results[url] = text
        elif len(text) < effective_min:
            results[url] = text
        else:
            to_filter.append((url, text))

    if not to_filter:
        return results

    n_workers = len(to_filter)
    logger.info('[ContentFilter] BATCH filtering %d/%d items  workers=%d',
                len(to_filter), len(items), n_workers)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(filter_web_content, text, url=url, query=query,
                        user_question=user_question, timeout=timeout,
                        min_chars=min_chars, config=config): url
            for url, text in to_filter
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as e:
                logger.error('[ContentFilter] BATCH item failed url=%s: %s',
                             url[:80], str(e)[:200], exc_info=True)
                results[url] = dict(to_filter).get(url, '')

    return results
