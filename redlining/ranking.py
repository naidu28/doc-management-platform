"""Bounded inputs for PostgreSQL's built-in cover-density ranking.

These are internal ranking sections, not retrieval passages. Trigram candidates
and exact source resolution remain authoritative for matching and anchors.
"""
import re

SECTION_WORDS = 256
OVERLAP_WORDS = 32


def ranking_sections(text):
    """Keep every ordinary word, including those at the end of huge documents.

    At most 256 lexemes of <2 KiB each keeps a vector below PostgreSQL's 1 MiB
    limit and positions below 16,384. Very long words cannot be ranked by PG;
    they remain searchable through exact matching. Overlap reduces boundary
    effects but is not a guarantee that all query terms share one section.
    """
    words = []
    for match in re.finditer(r'[^\W_]+', text):
        word = match.group()
        # Break the section at an unrankable token; do not invent a lexeme.
        if len(word.encode('utf-8')) >= 2048:
            if words:
                yield ' '.join(words)
                words = []
            continue
        words.append(word)
        if len(words) == SECTION_WORDS:
            yield ' '.join(words)
            words = words[-OVERLAP_WORDS:]
    if words:
        yield ' '.join(words)
