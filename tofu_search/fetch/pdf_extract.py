"""tofu_search.fetch.pdf_extract — PDF text extraction.

Uses pymupdf4llm (preferred) or pymupdf raw text as fallback.
"""

import re

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['extract_pdf_text']

MAX_PDF_BYTES = 100 * 1024 * 1024  # 100 MB

try:
    import pymupdf
    HAS_PYMUPDF = True
except ImportError:
    pymupdf = None
    HAS_PYMUPDF = False

try:
    import pymupdf4llm
    HAS_PYMUPDF4LLM = True
except ImportError:
    pymupdf4llm = None
    HAS_PYMUPDF4LLM = False


def _strip_manuscript_line_numbers(text):
    """Remove line numbers commonly found in review/manuscript PDFs."""
    lines = text.split('\n')
    non_blank = [l for l in lines if l.strip()]
    if len(non_blank) < 10:
        return text

    standalone_num = re.compile(r'^\s*\d{1,5}\s*$')
    num_count = sum(1 for l in non_blank if standalone_num.match(l))
    ratio = num_count / len(non_blank)

    if ratio > 0.15:
        cleaned = [l for l in lines if not standalone_num.match(l)]
        return '\n'.join(cleaned)

    leading_num = re.compile(r'^(\d{1,5})([ \t]{2,})(.*)')
    matches = [leading_num.match(l) for l in non_blank]
    leading_count = sum(1 for m in matches if m and len(m.group(3).strip()) > 0)
    leading_ratio = leading_count / len(non_blank)

    if leading_ratio > 0.25:
        nums = []
        for m in matches:
            if m and len(m.group(3).strip()) > 0:
                nums.append(int(m.group(1)))
        if len(nums) >= 5:
            increments = sum(1 for a, b in zip(nums, nums[1:]) if 0 < b - a <= 3)
            seq_ratio = increments / max(len(nums) - 1, 1)
            if seq_ratio > 0.4:
                def _strip_leading(line):
                    m = leading_num.match(line)
                    if m and len(m.group(3).strip()) > 0:
                        return m.group(3)
                    return line
                cleaned = [_strip_leading(l) for l in lines]
                return '\n'.join(cleaned)

    return text


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 0, url: str = '') -> str:
    """Extract text from PDF as Markdown.

    Strategy 1: pymupdf4llm -> Markdown with table/header preservation
    Strategy 2: pymupdf raw -> plain-text page-by-page fallback
    """
    if not HAS_PYMUPDF:
        return '[PDF extraction unavailable: pymupdf not installed]'

    if len(pdf_bytes) > MAX_PDF_BYTES:
        return f'[PDF too large: {len(pdf_bytes) // (1024*1024)} MB]'

    limit = max_chars if max_chars > 0 else 999_999_999

    if HAS_PYMUPDF4LLM:
        try:
            md_doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
            try:
                n = len(md_doc)
                chunks = pymupdf4llm.to_markdown(
                    md_doc, page_chunks=True, show_progress=False,
                    table_strategy="lines",
                )
            finally:
                md_doc.close()

            parts = []
            total = 0
            for ci, chunk in enumerate(chunks):
                page_md = chunk.get('text', '') if isinstance(chunk, dict) else str(chunk)
                page_md = _strip_manuscript_line_numbers(page_md)
                plen = len(page_md)
                if total + plen > limit:
                    remaining = limit - total
                    if remaining > 200:
                        parts.append(page_md[:remaining])
                    parts.append(f'\n[...truncated at {total + remaining:,} chars, page {ci + 1}/{n}]')
                    break
                parts.append(page_md)
                total += plen

            text = '\n\n---\n\n'.join(parts)
            logger.debug('pymupdf4llm OK: %d pages, %s chars — %s', n, f'{total:,}', url[:60])
            return text
        except Exception as e:
            logger.warning('pymupdf4llm failed, falling back to raw: %s', e, exc_info=True)

    # Strategy 2: raw pymupdf
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            n = len(doc)
            parts = []
            total = 0
            for page in doc:
                raw = page.get_text()
                total += len(raw)
                parts.append(raw)
                if limit < 999_999_999 and total > limit:
                    parts.append(f'\n[...truncated at {total:,} chars]')
                    break
        finally:
            doc.close()
        if not parts:
            return '[PDF: no extractable text]'
        full = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts))
        return full
    except Exception as e:
        logger.warning('[PDF] extraction failed for %s: %s', url[:80] if url else '?', e, exc_info=True)
        return f'[PDF extraction failed: {e}]'
