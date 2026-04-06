"""tofu_search.fetch.html_extract — HTML content & metadata extraction.

Contains HTML text extraction (trafilatura + BS4 fallback), link extraction,
publication date extraction, and code-hosting blob extraction (GitHub).
"""

import json
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import trafilatura

from tofu_search.fetch.utils import _get_bs4
from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'extract_html_publish_date',
    'extract_links_from_soup',
    'extract_html_text',
]


# ═══════════════════════════════════════════════════════
#  HTML publish date extraction
# ═══════════════════════════════════════════════════════

def extract_html_publish_date(html):
    """Extract publication date from HTML meta tags, JSON-LD, or <time> elements."""
    if not html:
        return ''

    from dateutil import parser as dateutil_parser

    def _try_parse(raw):
        if not raw or not isinstance(raw, str):
            return ''
        raw = raw.strip()
        if not raw:
            return ''
        try:
            dt = dateutil_parser.parse(raw, fuzzy=True)
            if dt.tzinfo is not None:
                from datetime import timezone
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            from datetime import datetime as _dt
            if dt.year < 2000 or dt > _dt.now() + timedelta(days=2):
                return ''
            return dt.strftime('%Y-%m-%d')
        except (ValueError, OverflowError):
            logger.debug('[Fetch] Date parse failed for raw=%r', raw)
            return ''

    try:
        soup = _get_bs4()(html[:50000], 'html.parser')
    except Exception as e:
        logger.debug('[Fetch] BS4 parse failed for publish date: %s', e)
        return ''

    meta_props = [
        'article:published_time', 'article:published',
        'og:article:published_time', 'og:published_time',
    ]
    for prop in meta_props:
        tag = soup.find('meta', attrs={'property': prop})
        if tag and tag.get('content'):
            r = _try_parse(tag['content'])
            if r:
                return r

    meta_names = [
        'pubdate', 'publishdate', 'publish_date', 'date',
        'DC.date', 'DC.date.issued', 'sailthru.date',
        'article.published', 'published_time', 'creation_date',
    ]
    for name in meta_names:
        tag = soup.find('meta', attrs={'name': re.compile(f'^{re.escape(name)}$', re.I)})
        if tag and tag.get('content'):
            r = _try_parse(tag['content'])
            if r:
                return r

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            raw_ld = (script.string or '').strip()
            if not raw_ld:
                continue
            ld = json.loads(raw_ld)
            items = ld if isinstance(ld, list) else [ld]
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ('datePublished', 'dateCreated', 'uploadDate'):
                    val = item.get(key)
                    if val:
                        r = _try_parse(val)
                        if r:
                            return r
                for g in (item.get('@graph') or []):
                    if isinstance(g, dict):
                        for key in ('datePublished', 'dateCreated'):
                            val = g.get(key)
                            if val:
                                r = _try_parse(val)
                                if r:
                                    return r
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue

    for time_tag in soup.find_all('time', attrs={'datetime': True}):
        r = _try_parse(time_tag['datetime'])
        if r:
            return r

    return ''


def extract_links_from_soup(soup, base_url=''):
    """Extract meaningful links from a parsed soup element."""
    links = []
    seen_urls = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
            continue
        if base_url and not href.startswith(('http://', 'https://')):
            href = urljoin(base_url, href)
        if href in seen_urls:
            continue
        seen_urls.add(href)
        text = a.get_text(strip=True)
        if not text or len(text) < 2:
            continue
        if text.lower() in ('home', 'back', 'top', 'skip', '\u2191', '\u2190', '\u2192', '\u2193', '#'):
            continue
        links.append(f'- [{text}]({href})')
    return links


# ═══════════════════════════════════════════════════════
#  Code-hosting blob extraction (GitHub)
# ═══════════════════════════════════════════════════════

_GITHUB_BLOB_URL_RE = re.compile(
    r'^https?://github\.com/[^/]+/[^/]+/blob/.+\.[a-zA-Z0-9]+$'
)

_GITHUB_EMBEDDED_RE = re.compile(
    r'<script\s+type="application/json"\s+'
    r'data-target="react-app\.embeddedData"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _find_nested_key(obj, key, depth=0):
    if depth > 12:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_nested_key(v, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_nested_key(v, key, depth + 1)
            if r is not None:
                return r
    return None


def _try_extract_github_blob(html_text, url):
    m = _GITHUB_EMBEDDED_RE.search(html_text)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Fetch] GitHub embeddedData JSON parse failed: %s', e)
        return None

    raw_lines = _find_nested_key(payload, 'rawLines')
    if not raw_lines or not isinstance(raw_lines, list):
        return None

    code = '\n'.join(raw_lines)
    if len(code) < 10:
        return None

    file_path = _find_nested_key(payload, 'path') or ''
    language = _find_nested_key(payload, 'language') or ''

    parts = []
    if file_path:
        header = f'File: {file_path}'
        if language:
            header += f'  ({language})'
        header += f'  -- {len(raw_lines)} lines'
        parts.append(header)
        parts.append('')
    parts.append(code)

    logger.debug('[Fetch] GitHub blob extracted: %s — %d lines, %d chars',
                 file_path or url[:80], len(raw_lines), len(code))
    return '\n'.join(parts)


def _try_extract_code_blob(html_text, url):
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None

    if host == 'github.com' and _GITHUB_BLOB_URL_RE.match(url):
        return _try_extract_github_blob(html_text, url)

    return None


# ═══════════════════════════════════════════════════════
#  HTML text extraction
# ═══════════════════════════════════════════════════════

def extract_html_text(html, max_chars, url=''):
    """Extract readable text from HTML. Preserves links as markdown section."""

    code = _try_extract_code_blob(html, url)
    if code:
        if max_chars and len(code) > max_chars:
            code = code[:max_chars] + '\n[...truncated]'
        return code

    main_text = None
    try:
        main_text = trafilatura.extract(html, include_comments=False, include_tables=True,
                                        no_fallback=False, favor_recall=True, deduplicate=True,
                                        include_links=True)
    except Exception as e:
        logger.warning('Trafilatura failed: %s', e, exc_info=True)

    try:
        soup = _get_bs4()(html, 'html.parser')
        for tag in ['script','style','nav','header','footer','aside','iframe',
                     'noscript','form','svg','button','input','select','textarea']:
            for el in soup.find_all(tag): el.decompose()
        for el in soup.find_all(attrs={'style': re.compile(r'display\s*:\s*none', re.I)}):
            el.decompose()
        for el in soup.find_all(attrs={'hidden': True}): el.decompose()
        for el in soup.find_all(class_=re.compile(
                r'\b(cookie|popup|modal|banner|advert|sidebar|menu|nav)\b', re.I)):
            el.decompose()
        main_el = None
        for c in [soup.find('main'), soup.find(role='main'),
                   soup.find(id=re.compile(r'^(content|main|article)', re.I)),
                   soup.find(class_=re.compile(r'^(post|article|content|main)', re.I)),
                   soup.find('article')]:
            if c and len(c.get_text(strip=True)) > 50: main_el = c; break

        content_el = main_el or soup.body or soup

        body_el = soup.body or soup
        content_links = extract_links_from_soup(content_el, base_url=url)
        content_urls = {l.split('](')[-1].rstrip(')') for l in content_links if '](' in l}
        body_links = extract_links_from_soup(body_el, base_url=url)
        extra_links = [l for l in body_links
                       if l.split('](')[-1].rstrip(')') not in content_urls]
        links = content_links + extra_links

        if not main_text:
            text = content_el.get_text(separator='\n', strip=True)
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            main_text = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))
            if not main_text or len(main_text) < 30:
                return None
    except Exception as e:
        logger.warning('BS4 extraction failed: %s', e, exc_info=True)
        if main_text:
            links = []
        else:
            return None

    if links and main_text:
        text_lower = main_text[:5000].lower() if main_text else ''
        if text_lower:
            def _link_score(link_md):
                score = 0
                parts = link_md.split('](')
                if len(parts) != 2:
                    return 0
                text_part = parts[0].lstrip('- [')
                url_part = parts[1].rstrip(')')
                path = url_part.split('/')[-1].replace('-', ' ').replace('_', ' ').lower()
                for word in path.split():
                    if len(word) > 3 and word in text_lower:
                        score += 3
                for word in text_part.lower().split():
                    if len(word) > 3 and word in text_lower:
                        score += 1
                return score
            scored = [(l, _link_score(l)) for l in links]
            scored.sort(key=lambda x: -x[1])
            selected = [l for l, s in scored[:80]]
        else:
            selected = links[:80]
        link_section = '\n\n--- Page Links ---\n' + '\n'.join(selected)
        combined = main_text + link_section
    else:
        combined = main_text

    if max_chars and len(combined) > max_chars:
        combined = combined[:max_chars] + '\n[...truncated]'
    return combined
