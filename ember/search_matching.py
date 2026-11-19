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


def source_text(text):
    return unicodedata.normalize('NFKC', text).casefold()


def source_phrase_pattern(query):
    """Literal words or phrases with flexible punctuation between tokens."""
    words = re.findall(r'[^\W_]+', source_text(query))
    if not words:
        return None
    # CJK writing does not delimit words with spaces. For Latin words, avoid
    # accepting a fragment such as "cat" inside "concatenate".
    before = r'(?<![a-z0-9])' if words[0][0].isascii() else ''
    after = r'(?![a-z0-9])' if words[-1][-1].isascii() else ''
    return re.compile(before + r'[\W_]+'.join(map(re.escape, words)) + after)


def source_sentences(text):
    return [part.strip() for part in re.split(r'(?<=[。！？!?；;])|\r?\n|(?<=\.)[ \t]+', text)
            if part.strip()]
