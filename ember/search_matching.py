"""Local text matching for active search, independent of embedding availability."""
import re
import unicodedata


def normalized(text):
    return ''.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', text).casefold()))


def text_matches(query, fields, *, terms=None):
    """Match whole summaries despite punctuation differences, or individual clauses."""
    text = unicodedata.normalize('NFKC', query).casefold()
    terms = set(re.findall(r'[^\W_]+', text)) if terms is None else terms
    phrase = normalized(query)
    bodies = [normalized(field) for field in fields if field]
    if not phrase:
        return 0, 0
    exact = int(any(phrase in body for body in bodies))
    hits = sum(any(term in body for body in bodies) for term in terms)
    return exact, hits
