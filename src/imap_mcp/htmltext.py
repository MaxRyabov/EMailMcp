"""HTML to plain text on the standard library (D11).

Hidden elements (the `hidden` attribute and inline styles that hide text) are
dropped with a marker: mail is written by third parties, and hidden text is a
classic carrier for instructions aimed at the model. If dropping would remove
more than half of the text, the markup is probably broken; the text is then
returned in full with a different marker.

Text hidden through CSS classes ("white on white") is not recognised.
"""

from __future__ import annotations

import re
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

_STYLE_HIDES = re.compile(
    r"(?:^|;)(?:display:none|visibility:hidden|mso-hide:all"
    r"|font-size:0(?:\.0*)?(?:px|pt|em|rem|%)?)(?:!important)?(?:;|$)"
)
_MAX_HEIGHT_0 = re.compile(r"(?:^|;)max-height:0(?:\.0*)?(?:px|pt|em|rem|%)?(?:!important)?(?:;|$)")
_OVERFLOW_HIDDEN = re.compile(r"(?:^|;)overflow:hidden(?:!important)?(?:;|$)")
_SPACES = re.compile(r"[ \t\r\f\v\u00a0]+")
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_REPEATED_MARK = re.compile(rf"{re.escape(HIDDEN_MARK)}(?:\s*{re.escape(HIDDEN_MARK)})+")


def _hides(attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    # CSS comments are whitespace to a mail client: /**/display:none still hides.
    style = _CSS_COMMENT.sub("", (attrs.get("style") or "").lower())
    style = re.sub(r"\s+", "", style)
    if not style:
        return False
    if _STYLE_HIDES.search(style):
        return True
    return bool(_MAX_HEIGHT_0.search(style) and _OVERFLOW_HIDDEN.search(style))


class _Element:
    __slots__ = ("tag", "hidden", "skip", "pre", "href", "start", "hidden_text")

    def __init__(self, tag, hidden, skip, pre, href, start):
        self.tag = tag
        self.hidden = hidden  # this element hides its content
        self.skip = skip
        self.pre = pre
        self.href = href
        self.start = start  # length of the visible output when the element opened
        self.hidden_text = False


class _Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[_Element] = []
        self.visible: list[str] = []  # output with hidden text removed
        self.full: list[str] = []  # output with hidden text kept (fallback)
        self.visible_chars = 0
        self.hidden_chars = 0
        self.pending_breaks = 0  # block boundaries merge: </div><div> is one line break

    def _in(self, attr: str) -> bool:
        return any(getattr(e, attr) for e in self.stack)

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

    def _pop_to(self, index: int) -> None:
        while len(self.stack) > index:
            self._close(self.stack.pop())

    def _close(self, el: _Element) -> None:
        if el.skip:
            return
        if el.hidden and el.hidden_text and not self._in("hidden"):
            self._emit(f" {HIDDEN_MARK} ", hidden=False)
            self.full.pop()  # the fallback keeps the hidden text itself, not the marker
        if el.href is not None:
            text = "".join(self.visible[el.start :]).strip()
            href = el.href
            if (
                text
                and href
                and not href.startswith(("#", "javascript:"))
                and href.removeprefix("mailto:") != text
            ):
                self._emit(f" ({href})", hidden=self._in("hidden") or el.hidden)
        if el.tag in _CELLS:
            self._emit("\t", hidden=False)
        elif el.tag in _BLOCK:
            self._block_break(double=el.tag in ("p", "table", "blockquote", "pre"))

    def _close_outermost(self, closes, bounds) -> None:
        found = None
        for i in range(len(self.stack) - 1, -1, -1):
            tag = self.stack[i].tag
            if tag in bounds:
                break
            if tag in closes:
                found = i
        if found is not None:
            self._pop_to(found)

    def _implicit_close(self, tag: str) -> None:
        if tag not in _HEAD_CONTENT:
            self._close_outermost({"head"}, set())
        if tag in _CLOSES_P:
            self._close_outermost({"p"}, _P_SCOPE)
        rule = _IMPLICIT.get(tag)
        if rule:
            self._close_outermost(*rule)

    def handle_starttag(self, tag, attrs):
        # A repeated attribute keeps its first value, as in browsers and mail clients.
        attrs = dict(reversed(attrs))
        self._implicit_close(tag)
        if tag == "br":
            self._emit("\n", hidden=self._in("hidden"))
            return
        if tag in _VOID:
            if tag == "hr":
                self._block_break()
            return
        if tag in _BLOCK and tag not in ("html", "body"):
            self._block_break()
        el = _Element(
            tag,
            hidden=_hides(attrs),
            skip=tag in _SKIP,
            pre=tag == "pre",
            href=(attrs.get("href") or "") if tag == "a" else None,
            start=len(self.visible),
        )
        self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.stack and self.stack[-1].tag == tag:
            self._close(self.stack.pop())

    def handle_endtag(self, tag):
        if tag == "br":
            self._emit("\n", hidden=self._in("hidden"))
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                self._pop_to(i)
                return

    def handle_data(self, data):
        if self._in("skip"):
            return
        if not self._in("pre"):
            data = _SPACES.sub(" ", data.replace("\n", " "))
        chars = len(data.strip())
        if self._in("hidden"):
            self.hidden_chars += chars
            if chars:
                for el in self.stack:
                    if el.hidden:
                        el.hidden_text = True
                        break
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
