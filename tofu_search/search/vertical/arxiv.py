"""arXiv vertical — paper metadata via the arXiv Atom API."""

import re
import xml.etree.ElementTree as ET

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _HEADERS, _TIMEOUT, logger

TYPE = 'arxiv'
DOMAIN = 'academic'

# Modern arXiv id: 2301.07041 / 2301.07041v2 (optionally prefixed "arXiv:").
_MODERN_RE = re.compile(r'(?:^|arxiv[:\s]+)(\d{4}\.\d{4,5}(?:v\d+)?)', re.IGNORECASE)
# Legacy id: hep-th/9901001, math.AG/0509025 (archive[.subclass]/YYMMnnn).
_LEGACY_RE = re.compile(
    r'(?:^|arxiv[:\s]+)([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)', re.IGNORECASE)


def detect(q):
    """Detect a modern or legacy arXiv identifier in the query."""
    m = _MODERN_RE.search(q)
    if m:
        return (TYPE, m.group(1), {})
    m = _LEGACY_RE.search(q)
    if m:
        return (TYPE, m.group(1), {})
    return None


def search(identifier, params):
    """Query arXiv API for paper details."""
    try:
        resp = base.http_get(
            'http://export.arxiv.org/api/query',
            params={'id_list': identifier, 'max_results': '1'},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            return None

        root = ET.fromstring(resp.text)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        entry = root.find('a:entry', ns)
        if entry is None:
            return None

        title = (entry.findtext('a:title', '', ns) or '').strip().replace('\n', ' ')
        summary = (entry.findtext('a:summary', '', ns) or '').strip()
        published = (entry.findtext('a:published', '', ns) or '')[:10]
        authors = [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)]
        pdf_links = [link.get('href') for link in entry.findall('a:link', ns)
                     if link.get('type') == 'application/pdf']
        categories = [c.get('term') for c in entry.findall('a:category', ns) if c.get('term')]

        parts = [f'## {title}', f'**arXiv**: {identifier}']
        if published:
            parts.append(f'**Published**: {published}')
        if authors:
            parts.append(f'**Authors**: {", ".join(authors[:10])}')
        if categories:
            parts.append(f'**Categories**: {", ".join(categories[:5])}')
        if pdf_links:
            parts.append(f'**PDF**: {pdf_links[0]}')
        parts.append(f'\n**Abstract**: {summary}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'arXiv'}
    except Exception as e:
        logger.warning('[Vertical] arXiv lookup failed for %s: %s', identifier, e)
        return None
