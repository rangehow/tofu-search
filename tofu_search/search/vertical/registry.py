"""Vertical registry — detection chain, handler tables, and dispatch.

Each vertical module exposes ``TYPE``, ``DOMAIN``, ``search(identifier, params)``
and (for the ones that participate in auto-detect) ``detect(query)``. This
module wires them into the ordered detection chain and the type/domain tables,
and provides the three public entry points used by the package façade.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tofu_search.log import get_logger
from tofu_search.search.vertical import (
    arxiv,
    cve,
    doi,
    github,
    hf_papers,
    ip,
    npm,
    pypi,
    semantic_scholar,
    stock,
)

logger = get_logger(__name__)

# All vertical modules.
_MODULES = [cve, arxiv, doi, pypi, npm, github, stock, ip, hf_papers, semantic_scholar]

# Type → search handler. Built from each module's TYPE + search().
_VERTICAL_HANDLERS = {m.TYPE: m.search for m in _MODULES}

# Ordered detection chain — ORDER IS SIGNIFICANT (most specific first, stock
# last). Mirrors the original detect_vertical_intent priority:
#   CVE → DOI → arXiv → PyPI → npm → GitHub → IP → HF papers → S2 → stock.
_DETECT_CHAIN = [cve, doi, arxiv, pypi, npm, github, ip, hf_papers, semantic_scholar, stock]

# Domain → list of types that belong to it. Used by the explicit-domain
# parameter path (`vertical='academic'` etc.) — order matters for fan-out.
_DOMAIN_TYPES = {
    'academic': ['arxiv', 'doi', 'hf_papers', 'semantic_scholar'],
    'code':     ['pypi', 'npm', 'github'],
    'finance':  ['stock'],
    'security': ['cve'],
    'network':  ['ip'],
}


def detect_vertical_intent(query):
    """Detect if a query matches a vertical domain pattern.

    Returns:
        (type, identifier, params) tuple, or None.
    """
    q = query.strip()
    if not q or len(q) > 200:
        return None
    for mod in _DETECT_CHAIN:
        hit = mod.detect(q)
        if hit:
            return hit
    return None


def list_domains():
    """Return the public list of supported vertical domains."""
    return list(_DOMAIN_TYPES.keys())


def search_vertical(domain_or_type, identifier, params=None):
    """Execute a vertical lookup.

    Backwards-compatible: ``domain_or_type`` may be a low-level type name
    (``'arxiv'``, ``'cve'``, …) — the legacy auto-detect path uses this.
    For explicit domain-level fan-out (``'academic'`` etc.), call
    :func:`search_vertical_domain` instead.

    Returns:
        Dict with keys (domain, type, identifier, content, source) or None.
    """
    handler = _VERTICAL_HANDLERS.get(domain_or_type)
    if not handler:
        logger.warning('[Vertical] Unknown type: %s', domain_or_type)
        return None

    t0 = time.time()
    result = handler(identifier, params or {})
    elapsed = time.time() - t0

    if result:
        logger.info('[Vertical] %s/%s OK in %.1fs (%d chars)',
                    domain_or_type, identifier, elapsed, len(result.get('content', '')))
    else:
        logger.info('[Vertical] %s/%s no data in %.1fs', domain_or_type, identifier, elapsed)
    return result


def _structured_items_from_record(record):
    """Best-effort extraction of frontend-friendly items from a handler record.

    Returns a list of dicts with at least ``title`` + ``url`` (when known)
    suitable for rendering as compact rows in the vertical card. Falls back
    to a single ``content``-bearing item so we never drop data.
    """
    if not isinstance(record, dict):
        return []
    items = record.get('items')
    if isinstance(items, list) and items:
        return items
    # Fallback: synthesize a single item from the record header.
    head = record.get('content', '').splitlines()[:1]
    title = head[0].lstrip('# ').strip() if head else (record.get('source') or record.get('type') or 'Result')
    return [{
        'title': title,
        'snippet': '',
        'url': '',
        'type': record.get('type') or '',
    }]


def _academic_subtypes_for(query):
    """Pick which academic sub-handlers to fan out for an explicit query.

    Strategy:
      - arXiv id present → ('arxiv', id) AND semantic_scholar citations
      - DOI present → ('doi', doi)
      - 'related to / similar to / citing X' phrase → semantic_scholar
      - 'trending / daily' phrase → hf_papers
      - free-text → hf_papers (keyword) AND semantic_scholar (related)
    """
    plans = []
    arxiv_m = re.search(r'\b(\d{4}\.\d{4,5}(?:v\d+)?)\b', query)
    if arxiv_m:
        plans.append(('arxiv', arxiv_m.group(1), {}))
        plans.append(('semantic_scholar', arxiv_m.group(1), {'mode': 'citations'}))
        return plans
    doi_m = re.search(r'(10\.\d{4,}/\S+)', query)
    if doi_m:
        plans.append(('doi', doi.strip_doi(doi_m.group(1)), {}))
        return plans

    ss_intent = semantic_scholar.detect(query)
    if ss_intent:
        plans.append(ss_intent)

    hf_intent = hf_papers.detect(query)
    if hf_intent:
        plans.append(hf_intent)

    if not plans:
        # Free-text academic query: fan out HF keyword + S2 related.
        plans.append(('hf_papers', query, {'period': 'day'}))
        plans.append(('semantic_scholar', query, {'mode': 'related'}))
    return plans


def _simple_subtypes_for(domain, query):
    """Default plan for non-academic domains: try every type in the domain."""
    return [(t, query, {}) for t in _DOMAIN_TYPES.get(domain, [])]


_DOMAIN_PLANNERS = {
    'academic': _academic_subtypes_for,
}


def search_vertical_domain(domain, query):
    """Run an explicit, domain-level vertical search.

    Fans out to one or more sub-handlers in parallel, merges their records,
    and returns a single dict::

        {
          'domain': 'academic',
          'sources': [{'type': 'hf_papers', 'source': 'Hugging Face Papers', ...}, ...],
          'items': [...],          # flat, frontend-renderable rows
          'content': '...',         # concatenated markdown for the LLM
        }

    or ``None`` if nothing useful came back. Safe against PDF parsing —
    every sub-handler returns JSON metadata only.
    """
    if domain not in _DOMAIN_TYPES:
        logger.warning('[Vertical] Unknown domain for explicit search: %s', domain)
        return None

    planner = _DOMAIN_PLANNERS.get(domain, _simple_subtypes_for)
    plans = planner(query) if planner is _academic_subtypes_for else planner(domain, query)
    if not plans:
        return None

    t0 = time.time()
    sources = []
    with ThreadPoolExecutor(max_workers=min(4, len(plans))) as pool:
        futs = {pool.submit(search_vertical, t, ident, params): (t, ident)
                for (t, ident, params) in plans}
        for fut in as_completed(futs):
            tname, ident = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                logger.warning('[Vertical] domain=%s sub=%s failed: %s', domain, tname, e)
                continue
            if rec:
                sources.append(rec)

    if not sources:
        logger.info('[Vertical] domain=%s no data in %.1fs (query=%r)',
                    domain, time.time() - t0, query[:80])
        return None

    # Merge: items per source + concatenated content for the LLM.
    items = []
    content_parts = []
    for rec in sources:
        sub_items = _structured_items_from_record(rec)
        for it in sub_items:
            it = dict(it)
            it.setdefault('type', rec.get('type', ''))
            it.setdefault('source', rec.get('source', ''))
            items.append(it)
        if rec.get('content'):
            content_parts.append(f"## {rec.get('source', rec.get('type', ''))}\n\n{rec['content']}")

    elapsed = time.time() - t0
    logger.info('[Vertical] domain=%s OK in %.1fs (%d sources, %d items)',
                domain, elapsed, len(sources), len(items))
    return {
        'domain': domain,
        'sources': [{'type': r.get('type'), 'source': r.get('source'),
                     'identifier': r.get('identifier')}
                    for r in sources],
        'items': items,
        'content': '\n\n'.join(content_parts),
    }
