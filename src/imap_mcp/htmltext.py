"""HTML to plain text on the standard library (D11).

Hidden elements are dropped with a marker: mail is written by third parties,
and hidden text is a classic carrier for instructions aimed at the model.
Recognised: the `hidden` attribute; the inline styles `display:none`,
`visibility:hidden` (or `collapse`), `font-size:0`, `mso-hide:all`, `max-height:0`
together with `overflow:hidden` (D11); elements a mail client does not render
(`iframe`, `datalist`, `noembed`, `noframes`, `dialog` without `open`, the body of
a `details` without `open`). If dropping would remove more than half of the text,
the markup is probably broken; the text is then returned in full with a
different marker.

Not recognised (residual risk): CSS classes ("white on white"), `opacity:0`,
off-screen positioning and other styles outside the list above.

The converter keeps per-tag indices and counters instead of scanning its element
stack, so deeply nested hostile markup costs linear time.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from html.parser import HTMLParser

HIDDEN_MARK = "[скрытый текст удалён]"
BROKEN_MARK = "[разметка некорректна, скрытый текст не удалён]"

_VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)
_SKIP = frozenset({"script", "style", "head", "title", "template", "noscript"})
# Never rendered by a mail client: content is hidden like display:none.
_NOT_RENDERED = frozenset({"iframe", "datalist", "noembed", "noframes"})
_BLOCK = frozenset(
    [
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
        "ul",
    ]
)
_CELLS = frozenset({"td", "th"})
# Opening one of these closes an open <p> (HTML "closes a p element in button scope").
_CLOSES_P = (_BLOCK - {"body", "html", "tr", "tbody", "tfoot", "thead"}) | {"select"}
_P_SCOPE = frozenset({"td", "th", "table", "caption", "button", "template", "html"})
# tag being opened -> (tags it closes implicitly, tags that bound the search).
# The outermost closable element below the nearest bound is closed with everything
# inside it: <tr> closes an open <td> together with its <tr>.
_IMPLICIT = {
    "a": ({"a"}, {"td", "th", "caption"}),
    "li": ({"li"}, {"ul", "ol", "menu", "table", "td", "th"}),
    "dt": ({"dt", "dd"}, {"dl", "table", "td", "th"}),
    "dd": ({"dt", "dd"}, {"dl", "table", "td", "th"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr", "td", "th"}, {"table", "tbody", "thead", "tfoot"}),
    "tbody": ({"tbody", "thead", "tfoot", "tr", "td", "th"}, {"table"}),
    "thead": ({"tbody", "thead", "tfoot", "tr", "td", "th"}, {"table"}),
    "tfoot": ({"tbody", "thead", "tfoot", "tr", "td", "th"}, {"table"}),
    "option": ({"option"}, {"select", "optgroup", "datalist"}),
    "optgroup": ({"option", "optgroup"}, {"select"}),
}
# Anything else opened inside <head> ends it.
_HEAD_CONTENT = frozenset(
    {"base", "link", "meta", "noscript", "script", "style", "template", "title"}
)

# A zero length in any spelling: 0, -0, 0.0, .0, 0px, 0cm, calc(0px).
_ZERO = r"[+-]?(?:0+(?:\.0*)?|\.0+)(?:px|pt|pc|em|rem|ex|ch|vh|vw|vmin|vmax|cm|mm|in|q|%)?"
_ZERO = rf"(?:{_ZERO}|calc\({_ZERO}\))"
_END = r"(?:!important)?(?:;|$)"
_STYLE_HIDES = re.compile(
    rf"(?:^|;)(?:display:none|visibility:(?:hidden|collapse)|mso-hide:all"
    rf"|font-size:{_ZERO}){_END}"
)
_MAX_HEIGHT_0 = re.compile(rf"(?:^|;)max-height:{_ZERO}{_END}")
_OVERFLOW_HIDDEN = re.compile(rf"(?:^|;)overflow:hidden{_END}")
_SPACES = re.compile(r"[ \t\r\f\v\u00a0]+")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-f]{1,6})\s?|(.))", re.DOTALL)
_REPEATED_MARK = re.compile(rf"{re.escape(HIDDEN_MARK)}(?:\s*{re.escape(HIDDEN_MARK)})+")


def _css_unescape(m: re.Match) -> str:
    if m[1]:
        cp = int(m[1], 16)
        return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else "\ufffd"
    return m[2]


def _hides(tag: str, attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs or tag in _NOT_RENDERED:
        return True
    if tag == "dialog" and "open" not in attrs:
        return True
    # A mail client reads CSS comments as whitespace and decodes escapes:
    # /**/display:none and display:non\65 still hide.
    style = _CSS_COMMENT.sub("", (attrs.get("style") or "").lower())
    style = _CSS_ESCAPE.sub(_css_unescape, style).lower()
    style = re.sub(r"\s+", "", style)
    if not style:
        return False
    if _STYLE_HIDES.search(style):
        return True
    return bool(_MAX_HEIGHT_0.search(style) and _OVERFLOW_HIDDEN.search(style))


class _Element:
    __slots__ = (
        "tag",
        "hidden",
        "collapsed",
        "reveal",
        "skip",
        "pre",
        "href",
        "start",
        "hidden_text",
    )

    def __init__(self, tag, *, hidden, collapsed, reveal, skip, pre, href, start):
        self.tag = tag
        self.hidden = hidden  # hides its content
        self.collapsed = collapsed  # <details> without open: hides all but its <summary>
        self.reveal = reveal  # the <summary> of a collapsed <details>
        self.skip = skip
        self.pre = pre
        self.href = href
        self.start = start  # length of the visible output when the element opened
        self.hidden_text = False  # hid some text; set on the innermost hiding element


class _Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.open_at: dict[str, list[int]] = {}  # tag -> stack indices, ascending
        self.hiding: list[_Element] = []  # open elements that hide or collapse
        self.skip_n = self.pre_n = self.hidden_n = self.collapsed_n = self.revealed_n = 0
        self.visible: list[str] = []  # output with hidden text removed
        self.full: list[str] = []  # output with hidden text kept (fallback)
        self.visible_chars = 0
        self.hidden_chars = 0
        self.pending_breaks = 0  # block boundaries merge: </div><div> is one line break

    def _hidden(self) -> bool:
        return self.hidden_n > 0 or self.collapsed_n > self.revealed_n

    def _emit(self, text: str, *, hidden: bool) -> None:
        if self.pending_breaks and text.strip():
            breaks = "\n" * self.pending_breaks
            self.pending_breaks = 0
            self.visible.append(breaks)
            self.full.append(breaks)
        if not hidden:
            self.visible.append(text)
        self.full.append(text)

    def _block_break(self, double: bool = False) -> None:
        self.pending_breaks = max(self.pending_breaks, 2 if double else 1)

    def _push(self, el: _Element) -> None:
        self.open_at.setdefault(el.tag, []).append(len(self.stack))
        self.stack.append(el)
        self.skip_n += el.skip
        self.pre_n += el.pre
        self.hidden_n += el.hidden
        self.collapsed_n += el.collapsed
        self.revealed_n += el.reveal
        if el.hidden or el.collapsed:
            self.hiding.append(el)

    def _pop_to(self, index: int) -> None:
        while len(self.stack) > index:
            el = self.stack.pop()
            self.open_at[el.tag].pop()
            self.skip_n -= el.skip
            self.pre_n -= el.pre
            self.hidden_n -= el.hidden
            self.collapsed_n -= el.collapsed
            self.revealed_n -= el.reveal
            if el.hidden or el.collapsed:
                self.hiding.pop()
            self._close(el)

    def _close(self, el: _Element) -> None:
        if el.skip:
            return
        if el.hidden_text:
            if self._hidden() and self.hiding:
                self.hiding[-1].hidden_text = True  # one marker, from the outermost
            else:
                self._emit(f" {HIDDEN_MARK} ", hidden=False)
                self.full.pop()  # the fallback keeps the hidden text itself, not the marker
        if el.href is not None:
            # A link whose text is all hidden gets no URL either.
            text = "".join(self.visible[el.start :]).replace(HIDDEN_MARK, "").strip()
            href = el.href
            scheme = href.lower()  # URL schemes are case-insensitive
            if (
                text
                and href
                and not scheme.startswith(("#", "javascript:"))
                and (href[7:] if scheme.startswith("mailto:") else href) != text
            ):
                self._emit(f" ({href})", hidden=self._hidden())
        if el.tag in _CELLS:
            self._emit("\t", hidden=False)
        elif el.tag in _BLOCK:
            self._block_break(double=el.tag in ("p", "table", "blockquote", "pre"))

    def _close_outermost(self, closes, bounds) -> None:
        bound = max((self.open_at[b][-1] for b in bounds if self.open_at.get(b)), default=-1)
        found = None
        for tag in closes:
            indices = self.open_at.get(tag)
            if indices and indices[-1] > bound:
                first = indices[bisect_right(indices, bound)]
                found = first if found is None else min(found, first)
        if found is not None:
            self._pop_to(found)

    def _implicit_close(self, tag: str) -> None:
        if tag not in _HEAD_CONTENT:
            self._close_outermost(("head",), ())
        if tag in _CLOSES_P:
            self._close_outermost(("p",), _P_SCOPE)
        rule = _IMPLICIT.get(tag)
        if rule:
            self._close_outermost(*rule)

    def handle_starttag(self, tag, attrs):
        # A repeated attribute keeps its first value, as in browsers and mail clients.
        attrs = dict(reversed(attrs))
        self._implicit_close(tag)
        if tag == "br":
            self._emit("\n", hidden=self._hidden())
            return
        if tag in _VOID:
            if tag == "hr":
                self._block_break()
            return
        if tag in _BLOCK and tag not in ("html", "body"):
            self._block_break()
        hidden = _hides(tag, attrs)
        parent = self.stack[-1] if self.stack else None
        self._push(
            _Element(
                tag,
                hidden=hidden,
                collapsed=tag == "details" and "open" not in attrs and not hidden,
                reveal=tag == "summary" and parent is not None and parent.collapsed,
                skip=tag in _SKIP,
                pre=tag == "pre",
                href=(attrs.get("href") or "") if tag == "a" else None,
                start=len(self.visible),
            )
        )

    def handle_startendtag(self, tag, attrs):
        # HTML ignores the slash on a non-void element: <span hidden/>text</span>
        # hides the text in a mail client, so it must here too.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "br":
            self._emit("\n", hidden=self._hidden())
            return
        indices = self.open_at.get(tag)
        if indices:
            self._pop_to(indices[-1])

    def handle_data(self, data):
        if self.skip_n:
            return
        if not self.pre_n:
            data = _SPACES.sub(" ", data.replace("\n", " "))
        chars = len(data.strip())
        if self._hidden():
            self.hidden_chars += chars
            if chars:
                self.hiding[-1].hidden_text = True
            self._emit(data, hidden=True)
        else:
            self.visible_chars += chars
            self._emit(data, hidden=False)

    def finish(self) -> None:
        self.close()
        self._pop_to(0)


def _tidy(text: str) -> str:
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _REPEATED_MARK.sub(HIDDEN_MARK, text)
    return text.strip()


def html_to_text(html: str) -> str:
    conv = _Converter()
    conv.feed(html)
    conv.finish()
    total = conv.visible_chars + conv.hidden_chars
    if conv.hidden_chars * 2 > total:
        return _tidy("".join(conv.full)) + "\n\n" + BROKEN_MARK
    return _tidy("".join(conv.visible))
