"""Semantically complete, safely-highlightable snippet previews for notice body text.

Public API
----------
build_preview(body, query='', ai_summary='', max_fragments=2) -> dict

Returns a dict with:
    text:               str   Concatenated fragment text.  The Chinese six-dot
                              ellipsis '\u2026\u2026' is inserted only where body
                              content has actually been omitted (before the
                              first fragment when head_omitted, after the last
                              fragment when tail_omitted, and between
                              fragments).
    is_search_context:  bool  True iff a non-empty query was supplied AND at
                              least one match was located in the body.
    fragments:          list  One entry per fragment:
                              text:    the fragment text (raw, unescaped)
                              start:   absolute start index in body (inclusive)
                              end:     absolute end index in body (exclusive)
                              matches: list of (start, end) tuples within the
                                       body that fall inside this fragment.
                              Each fragment is guaranteed to satisfy
                                       body[fragment.start:fragment.end]
                                       == fragment['text'].
    head_omitted:       bool  True if the first fragment does not begin at
                              body[0].
    tail_omitted:       bool  True if the last fragment does not end at
                              body[-1].

The module never injects HTML markup.  The frontend can highlight by
reading the body text and wrapping fragment.matches intervals via DOM
range APIs -- no HTML escaping is required and no unescaped HTML is ever
present in the returned strings.

Design rules (matches the spec)
-------------------------------
* No query:     prefer a non-empty ai_summary; otherwise collapse runs of
                whitespace and pick the first meaningful paragraph or
                complete sentence.
* Query hits:   expand each match to the nearest complete sentence using
                strong punctuation (Chinese 'full stop', 'excl', 'qmark',
                'semi' plus ASCII '.!?;' and line breaks).  When the
                expansion would exceed the per-fragment length budget,
                shrink at weak punctuation (Chinese comma, list-sep,
                colon plus ASCII ',:').  The '\u224820 chars' anchor around
                the keyword is only a search window for the initial
                boundary probe -- never a hard cut.
* Protected regions (must not be cut): document numbers such as
                教字〔2026〕175号, URLs, emails, long digit / date /
                phone sequences, English (or mixed-case) words, paired
                brackets 《...》 / （...） / [...] .
* Same- or adjacent-sentence hits merge into a single fragment; when
                more fragments survive than max_fragments, the spread-out
                first and last are kept (and evenly-spaced middles when
                the cap is greater than two).
* The ellipsis '……' only appears at positions where body content has
                actually been omitted.
* When no punctuation is available at all, the snippet is bounded by a
                safe upper length and snapped to the nearest token
                boundary.
"""

from __future__ import annotations

import re
from typing import Any


# ---- Punctuation classes -------------------------------------------------

# Strong sentence-end punctuation (Chinese + ASCII) plus line breaks.
_STRONG_CHARS = '\u3002\uff01\uff1f\uff1b.!?;\n\r'
# Weak in-sentence punctuation (commas, pauses, enumeration marks, colon).
_WEAK_CHARS = '\uff0c\u3001\uff1a,:\u3001'
_STRONG_END_RE = re.compile('[' + re.escape(_STRONG_CHARS) + ']+')
_WEAK_RE = re.compile('[' + re.escape(_WEAK_CHARS) + ']+')
# Collapse runs of whitespace (incl. full-width space) -- only used by the
# no-query fallback when normalising the body.
_WS_RUN = re.compile(r'[\s\u3000]+')


# ---- Protected regions (must NOT be split) ------------------------------

# Each pattern below is a top-level branch of the alternation.
_PROTECTED_PATTERNS = [
    # Chinese document number, e.g. 教字〔2026〕175号 / 京政发〔2026〕15号 /
    # 〔2026〕第5号 / 教高函字〔2026〕第3号 ...
    (
        r'[\u4e00-\u9fff]{0,10}\s*[〔\[\(【]\s*[\d\u4e00-\u9fff]{1,8}\s*'
        r'[〕\]\)】]\s*[第]?\s*\d{1,6}\s*号'
    ),
    # Common attachment filenames; the extension dot is not a sentence end.
    (
        r'[^\s，。！？；：,!?;:]{1,100}\.'
        r'(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|csv|txt)(?=$|[\s，。！？；：,!?;:])'
    ),
    # URLs -- http/https/ftp.  The character class stops at typical
    # terminators such as CJK / fullwidth punctuation and whitespace.
    (
        r'https?://[A-Za-z0-9._~:/?#\[\]@!$&' + "'" + r'()*+,;=%\-]+|'
        r'ftp://[A-Za-z0-9._~:/?#\[\]@!$&' + "'" + r'()*+,;=%\-]+'
    ),
    # Email addresses.
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
    # Long digit / date / phone sequences (4+ chars).
    r'\d[\d\-\.\/\u2013\u2014:]{3,}',
    # Book titles 《...》
    r'《[^》\n\r]{1,120}》',
    # Chinese parentheses （...）
    r'（[^）\n\r]{1,400}）',
    # Square brackets [...]
    r'\[[^\]\n\r]{1,120}\]',
    # English / mixed-case words (3+ chars).
    r'[A-Za-z][A-Za-z0-9_\-]{2,}',
]
_PROTECTED_RE = re.compile('|'.join('(?:' + p + ')' for p in _PROTECTED_PATTERNS))


# ---- Length budgets (body characters) ----------------------------------

# Hard upper bound on a single fragment after every adjustment.
_HARD_MAX = 200
# Per-fragment length budget when picking a strong-punctuation boundary.
_TARGET = 160
# Per-fragment length budget when shrinking at weak punctuation.
_SHRINK = 110
# Used as a last-resort length when no punctuation exists at all.
_FALLBACK = 140


# ---- Token / boundary arithmetic ---------------------------------------


def _is_token_char(ch):
    '''Characters that belong to a contiguous token we don't want to split.'''
    if not ch:
        return False
    code = ord(ch)
    if 0x4E00 <= code <= 0x9FFF:  # CJK Unified Ideographs
        return True
    if 0x3400 <= code <= 0x4DBF:  # CJK Extension A
        return True
    if 0x3040 <= code <= 0x309F:  # Hiragana
        return True
    if 0x30A0 <= code <= 0x30FF:  # Katakana
        return True
    return ch.isalnum() or ch in '_-\u2013\u2014'


def _snap_to_token_start(body, pos, min_pos=0):
    '''Move pos backward to the start of the current token, never below min_pos.'''
    i = pos
    while i > min_pos and _is_token_char(body[i - 1]):
        i -= 1
    return i


def _snap_to_token_end(body, pos, max_pos=None):
    '''Move pos forward to the end of the current token, capped at max_pos.'''
    upper = len(body) if max_pos is None else max_pos
    i = pos
    while i < upper and _is_token_char(body[i]):
        i += 1
    return i


def _push_out_of_protected(body, pos, side):
    '''If pos is strictly inside a protected span, push it to the nearest edge.

    side='before' -> push backward (return the span start, le original pos).
    side='after'  -> push forward  (return the span end,   ge original pos).
    '''
    for m in _PROTECTED_RE.finditer(body):
        s, e = m.start(), m.end()
        if s < pos < e:
            return s if side == 'before' else e
    return pos


def _first_strong_after(body, pos, max_dist):
    '''Return the start of the next sentence boundary ge pos, or None.

    The boundary is the position immediately after a strong-punctuation run
    (plus any whitespace following it).  Returns None if no such boundary
    exists within max_dist characters.
    '''
    span_end = min(len(body), pos + max_dist)
    for m in _STRONG_END_RE.finditer(body, pos, span_end):
        nxt = m.end()
        if _push_out_of_protected(body, nxt, 'after') != nxt:
            continue
        while nxt < span_end and body[nxt] in ' \t\u3000':
            nxt += 1
        return nxt
    return None


def _last_strong_before(body, pos, max_dist):
    '''Return the start of the sentence immediately preceding pos, or None.

    Equivalent to the position right after the closest strong-punctuation run
    before pos (and any whitespace that follows it).
    '''
    start = max(0, pos - max_dist)
    last = None
    for m in _STRONG_END_RE.finditer(body, start, pos):
        if m.end() <= pos:
            candidate = m.end()
            while candidate < pos and body[candidate] in ' \t\u3000':
                candidate += 1
            last = candidate
    return last


def _first_weak_after(body, pos, max_dist):
    span_end = min(len(body), pos + max_dist)
    for m in _WEAK_RE.finditer(body, pos, span_end):
        nxt = m.end()
        if _push_out_of_protected(body, nxt, 'after') != nxt:
            continue
        while nxt < span_end and body[nxt] in ' \t\u3000':
            nxt += 1
        return nxt
    return None


def _last_weak_before(body, pos, max_dist):
    start = max(0, pos - max_dist)
    last = None
    for m in _WEAK_RE.finditer(body, start, pos):
        if m.end() <= pos:
            candidate = m.end()
            while candidate < pos and body[candidate] in ' \t\u3000':
                candidate += 1
            last = candidate
    return last


def _clamp(value, lo, hi):
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---- Per-match expansion ------------------------------------------------


def _expand_match(body, m_start, m_end):
    '''Expand (m_start, m_end) to a clean, semantically complete snippet.

    Returns (start, end) absolute offsets into body.
    '''
    n = len(body)

    # -- End of snippet (look AHEAD from match end) -----------------------
    end = _first_strong_after(body, m_end, _TARGET)

    if end is None:
        # No strong punctuation within target -- try weak.
        end = _first_weak_after(body, m_end, _SHRINK)
        if end is None or end - m_start > _HARD_MAX:
            # No punctuation, or weak still too long -> safe upper bound.
            end = _clamp(m_end + _FALLBACK, m_end, n)
            end = _snap_to_token_end(body, end, n)
    elif end - m_start > _HARD_MAX:
        # Strong punctuation found but the result is too long -- shrink weak.
        shrunk = _first_weak_after(body, m_end, _SHRINK)
        if shrunk is not None and shrunk - m_start <= _HARD_MAX:
            end = shrunk
        else:
            end = _clamp(m_start + _HARD_MAX, m_end, n)
            end = _snap_to_token_end(body, end, n)

    end = _push_out_of_protected(body, end, 'after')
    end = _clamp(end, m_end, n)
    if end < m_end:
        end = m_end

    # -- Start of snippet (look BACKWARD from match start) ---------------
    start = _last_strong_before(body, m_start, _TARGET)

    if start is None:
        start = _last_weak_before(body, m_start, _SHRINK)
        if start is None or m_end - start > _HARD_MAX:
            start = _clamp(m_start - _FALLBACK, 0, m_start)
            start = _snap_to_token_start(body, start, 0)
    elif m_end - start > _HARD_MAX:
        shrunk = _last_weak_before(body, m_start, _SHRINK)
        if shrunk is not None and m_end - shrunk <= _HARD_MAX:
            start = shrunk
        else:
            start = _clamp(m_start - _HARD_MAX, 0, m_start)
            start = _snap_to_token_start(body, start, 0)

    start = _push_out_of_protected(body, start, 'before')
    start = _clamp(start, 0, m_start)
    if start > m_start:
        start = m_start

    if end <= start:
        end = min(n, start + 1)

    # Final per-fragment cap: ensure |snippet| le HARD_MAX by trimming end first,
    # then start, never crossing the match itself.
    if end - start > _HARD_MAX:
        end_target = start + _HARD_MAX
        new_end = min(end, end_target)
        new_end = _snap_to_token_end(body, new_end, n)
        new_end = _push_out_of_protected(body, new_end, 'after')
        new_end = _clamp(new_end, m_end, n)
        if new_end - start <= _HARD_MAX:
            end = new_end
        else:
            end = _clamp(end_target, m_end, n)
            end = _snap_to_token_end(body, end, n)
    if end - start > _HARD_MAX:
        start_target = end - _HARD_MAX
        new_start = max(start, start_target)
        new_start = _snap_to_token_start(body, new_start, 0)
        new_start = _push_out_of_protected(body, new_start, 'before')
        new_start = _clamp(new_start, 0, m_start)
        if end - new_start <= _HARD_MAX:
            start = new_start
        else:
            start = max(0, end - _HARD_MAX)

    return start, end


# ---- Match finding ------------------------------------------------------


def _find_matches(body, query):
    '''Find positions where query (or one of its >=2-char tokens) appears in body.

    ASCII matches are case-insensitive.
    '''
    if not query or not body:
        return []

    needle = query.strip()
    if not needle:
        return []

    # First try the full query as a (case-insensitive for ASCII) substring.
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    matches = [(m.start(), m.end()) for m in pattern.finditer(body)]
    if matches:
        return _dedupe_overlapping(matches)

    # Fall back: split the query into whitespace tokens, >= 2 chars each.
    tokens = [t for t in re.split(r'\s+', needle) if t and len(t) >= 2]
    seen = set()
    unique = []
    for t in tokens:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)
    for t in unique:
        tp = re.compile(re.escape(t), re.IGNORECASE)
        for m in tp.finditer(body):
            matches.append((m.start(), m.end()))

    matches.sort()
    return _dedupe_overlapping(matches)


def _dedupe_overlapping(matches):
    '''Collapse overlapping or touching spans; keep the longest end.'''
    if not matches:
        return []
    out = [matches[0]]
    for s, e in matches[1:]:
        ps, pe = out[-1]
        if s < pe:
            out[-1] = (ps, max(pe, e))
        else:
            out.append((s, e))
    return out


# ---- Fragment selection -------------------------------------------------


def _merge_adjacent(fragments):
    '''Merge fragments that overlap or sit immediately next to each other.

    A gap of at most 1 character (typically a single trailing whitespace)
    is treated as adjacent.
    '''
    if not fragments:
        return []
    merged = [list(fragments[0])]
    for s, e in fragments[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(m) for m in merged]


def _cap_fragment(body, start, end, hard_max):
    '''Cap a merged fragment to hard_max, snapping to token boundaries.'''
    if end - start <= hard_max:
        return start, end
    end_target = start + hard_max
    new_end = min(end, end_target)
    new_end = _snap_to_token_end(body, new_end, len(body))
    new_end = _push_out_of_protected(body, new_end, 'after')
    if new_end - start <= hard_max + 8:
        return start, new_end
    start_target = end - hard_max
    new_start = max(start, start_target)
    new_start = _snap_to_token_start(body, new_start, 0)
    new_start = _push_out_of_protected(body, new_start, 'before')
    if end - new_start <= hard_max + 8:
        return new_start, end
    return max(0, end - hard_max), end


def _select(fragments, limit, body=None, hard_max=None):
    '''Cap fragment count to limit; also cap each fragment length.'''
    if hard_max is None:
        hard_max = _HARD_MAX
    if limit <= 0:
        chosen = list(fragments[:1]) if fragments else []
    elif len(fragments) <= limit:
        chosen = list(fragments)
    elif limit == 1:
        chosen = [fragments[0]]
    else:
        n = len(fragments)
        idxs = sorted({0, n - 1, *(round((n - 1) * i / (limit - 1)) for i in range(1, limit - 1))})
        chosen = [fragments[i] for i in idxs]
    if body is not None:
        chosen = [tuple(_cap_fragment(body, s, e, hard_max)) for s, e in chosen]
    return chosen


def _is_heading_only_paragraph(text):
    '''True for document-number/salutation lines that make poor card previews.'''
    normalized = _WS_RUN.sub(' ', text or '').strip()
    if not normalized:
        return True
    protected = _PROTECTED_RE.fullmatch(normalized)
    if protected and '号' in normalized:
        return True
    if len(normalized) <= 32 and normalized.endswith(('：', ':')):
        return True
    return normalized in {'公告正文', '通知公告', '公示', '通知'}


def _complete_opening(paragraph):
    first_para = _WS_RUN.sub(' ', paragraph or '').strip()
    if not first_para:
        return ''

    end = _first_strong_after(first_para, 0, _HARD_MAX)
    if end is not None:
        return first_para[:end].rstrip()

    weak_probe = min(40, len(first_para)) if len(first_para) > 40 else 0
    end = _first_weak_after(first_para, weak_probe, max(1, _FALLBACK - weak_probe))
    if end is not None and end <= _FALLBACK + 20:
        return first_para[:end].rstrip()

    end = min(len(first_para), _FALLBACK)
    end = _snap_to_token_end(first_para, end, len(first_para))
    return first_para[:end].rstrip()


def _first_meaningful(body):
    '''No-query fallback: first substantive paragraph / complete sentence.'''
    if not body or not body.strip():
        return ''

    # HTML-to-text extraction uses one newline between block elements on
    # several school sites, while pasted/plain text often uses blank lines.
    chunks = [chunk.strip() for chunk in re.split(r'\r?\n+', body.strip()) if chunk.strip()]
    for chunk in chunks:
        if not _is_heading_only_paragraph(chunk):
            return _complete_opening(chunk)
    return _complete_opening(chunks[0] if chunks else body)


# ---- Public API ---------------------------------------------------------


def build_preview(body, query='', ai_summary='', max_fragments=2):
    '''Build a semantically complete snippet preview for a notice body.

    See the module docstring for the returned dict shape and behaviour
    contract.
    '''
    body = body or ''
    query = (query or '').strip()
    ai_summary = (ai_summary or '').strip()
    if max_fragments < 1:
        max_fragments = 1

    fallback_empty = {
        'text': ai_summary,
        'is_search_context': False,
        'fragments': [],
        'head_omitted': False,
        'tail_omitted': False,
    }
    if not body.strip():
        return fallback_empty

    # -- No query -> use ai_summary (preferred) or first meaningful paragraph
    if not query:
        text = ai_summary or _first_meaningful(body)
        out_frags = []
        if text:
            out_frags.append({'text': text, 'start': 0, 'end': 0, 'matches': []})
        return {
            'text': text,
            'is_search_context': False,
            'fragments': out_frags,
            'head_omitted': False,
            'tail_omitted': False,
        }

    matches = _find_matches(body, query)

    if not matches:
        # Query present but nothing matched -- gracefully degrade.
        text = ai_summary or _first_meaningful(body)
        out_frags = [{'text': text, 'start': 0, 'end': 0, 'matches': []}] if text else []
        return {
            'text': text,
            'is_search_context': False,
            'fragments': out_frags,
            'head_omitted': False,
            'tail_omitted': False,
        }

    # -- Search context: expand, merge, select, and assemble ------------
    expanded = [_expand_match(body, s, e) for s, e in matches]
    merged = _merge_adjacent(expanded)
    chosen = _select(merged, max_fragments, body=body)

    out_fragments = []
    for s, e in chosen:
        in_matches = [(ms, me) for (ms, me) in matches if ms >= s and me <= e]
        out_fragments.append({
            'text': body[s:e],
            'start': s,
            'end': e,
            'matches': in_matches,
        })

    text = '\u2026\u2026'.join(f['text'] for f in out_fragments)
    head = bool(out_fragments) and out_fragments[0]['start'] > 0
    tail = bool(out_fragments) and out_fragments[-1]['end'] < len(body)

    if head:
        text = '\u2026\u2026' + text
    if tail:
        text = text + '\u2026\u2026'

    return {
        'text': text,
        'is_search_context': True,
        'fragments': out_fragments,
        'head_omitted': head,
        'tail_omitted': tail,
    }
