"""tofu_search.search.format — Formatting of search results."""

__all__ = ['format_search_for_tool_response']


def format_search_for_tool_response(results, search_diag=None):
    """Format search results into a human/model-readable string.

    Args:
        results: List of search result dicts (irrelevant pages already removed).
        search_diag: Optional diagnostic dict when 0 results were found.
    """
    if not results:
        if search_diag:
            reason = search_diag.get('reason', 'unknown')
            detail = search_diag.get('reason_detail', '')
            if reason == 'network_error':
                return ("Search failed: all search engines encountered network errors. "
                        "The server may have limited internet connectivity.")
            elif reason == 'partial_network_error':
                return ("Search returned 0 results. %s "
                        "Try rephrasing the query." % detail)
            else:
                return ("Search returned 0 results -- no matching content was found. "
                        "Try rephrasing with different keywords.")
        return "No search results found."

    parts = []
    for i, r in enumerate(results, 1):
        entry = (f"[{i}] {r['title']}\n"
                 f"    URL: {r['url']}\n"
                 f"    Source: {r['source']}")

        if r.get('full_content'):
            entry += (f"\n\n    ---- Full Page Content "
                      f"({len(r['full_content']):,} chars) ----\n"
                      f"{r['full_content']}")
        else:
            entry += (f"\n    Summary: {r['snippet']}"
                      f'\n    (Full content not available)')

        parts.append(entry)

    header = "Search results:\n\n"
    return header + "\n\n============================\n\n".join(parts)
