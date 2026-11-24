"""Resolve CC writer quotes against registered originals in chronological order."""
from .source_paths import QuoteSourceError
from .search_matching import normalized

_NEARBY_MESSAGES = 12


def _distance(quote, text):
    """Levenshtein distance to the closest contiguous substring."""
    previous = list(range(len(quote) + 1))
    best = previous[-1]
    for ch in text:
        current = [0]
        for i, expected in enumerate(quote, 1):
            current.append(min(previous[i] + 1, current[-1] + 1, previous[i-1] + (ch != expected)))
        previous = current
        best = min(best, current[-1])
    return best


class QuoteMatcher:
    def __init__(self, rows):
        self.originals = [row['content'] for row in rows]
        self.normalized = [normalized(text) for text in self.originals]

    def find(self, quote, indices, *, nearby=None):
        exact = next((i for i in indices if quote in self.originals[i]), None)
        if exact is not None:
            return exact
        phrase = normalized(quote)
        if not phrase:
            return None
        exact = next((i for i in indices if phrase in self.normalized[i]), None)
        if exact is not None or nearby is None or not 5 <= len(phrase) <= 120:
            return exact
        limit = min(3, max(1, len(phrase) // 10))
        candidates = [(_distance(phrase, self.normalized[i]), abs(i-nearby), i)
                      for i in indices if abs(i-nearby) <= _NEARBY_MESSAGES
                      and len(self.normalized[i]) >= len(phrase)-limit]
        best = min(candidates, default=None)
        return best[2] if best and best[0] <= limit else None

    def resolve(self, span, focus):
        n = len(self.originals)
        newest = range(n-1, -1, -1)
        start_quote, end_quote = span['start_quote'], span['end_quote']
        # A known focus or end disambiguates repeated start quotes.
        focus_at = self.find(focus, newest) if focus else None
        end_at = self.find(end_quote, newest)
        anchor = focus_at if focus_at is not None else end_at
        exact_starts = [i for i in newest if start_quote in self.originals[i]]
        start_at = (exact_starts[0] if len(exact_starts) == 1
                    else self.find(start_quote, newest if anchor is None else range(anchor, -1, -1)))
        if start_at is not None:
            if focus:
                focus_at = self.find(focus, range(start_at, n), nearby=start_at)
            lower = focus_at if focus_at is not None else start_at
            end_at = self.find(end_quote, range(lower, n), nearby=lower)
        elif anchor is not None:
            start_at = self.find(start_quote, range(anchor, -1, -1), nearby=anchor)
            if focus_at is not None:
                end_at = self.find(end_quote, range(focus_at, n), nearby=focus_at)
            elif focus and end_at is not None:
                focus_at = self.find(focus, range(end_at, -1, -1), nearby=end_at)
        matches = [i for i in (start_at, focus_at, end_at) if i is not None]
        return (min(matches), max(matches), focus_at) if matches else None


def resolve_spans(spans, focus, rows):
    matcher = QuoteMatcher(rows)
    selected = []
    focus_at = None
    for span in spans:
        match = matcher.resolve(span, focus if len(spans) == 1 else None)
        if match is None:
            continue
        lo, hi, current_focus = match
        selected.extend(range(lo, hi+1))
        if current_focus is not None:
            focus_at = current_focus
    if not selected:
        raise QuoteSourceError('quote_not_found')
    if focus_at is None:
        focus_at = matcher.find(focus, sorted(set(selected), reverse=True), nearby=selected[0])
    return [rows[i] for i in sorted(set(selected))], rows[focus_at if focus_at is not None else selected[0]]
