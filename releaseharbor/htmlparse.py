"""Narrow HTML extraction for trusted-shape fixture portal pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Anchor:
    attrs: dict[str, str]
    text: str = ""


@dataclass
class ParsedPage:
    main_attrs: dict[str, str] = field(default_factory=dict)
    anchors: list[Anchor] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)


class _PortalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage()
        self._anchor_attrs: dict[str, str] | None = None
        self._anchor_text: list[str] = []
        self._heading_tag: str | None = None
        self._heading_text: list[str] = []

    @staticmethod
    def _attrs(raw_attrs) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for name, value in raw_attrs:
            key = name.lower()
            if key not in attrs:
                attrs[key] = value if value is not None else ""
        return attrs

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        attributes = self._attrs(attrs)
        if lowered == "main" and not self.page.main_attrs:
            self.page.main_attrs = attributes
        elif lowered == "a":
            self._anchor_attrs = attributes
            self._anchor_text = []
        elif lowered in {"h1", "h2"}:
            self._heading_tag = lowered
            self._heading_text = []

    def handle_data(self, data: str) -> None:
        if self._anchor_attrs is not None:
            self._anchor_text.append(data)
        if self._heading_tag is not None:
            self._heading_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "a" and self._anchor_attrs is not None:
            self.page.anchors.append(
                Anchor(self._anchor_attrs, " ".join("".join(self._anchor_text).split()))
            )
            self._anchor_attrs = None
            self._anchor_text = []
        elif lowered == self._heading_tag and self._heading_tag is not None:
            text = " ".join("".join(self._heading_text).split())
            if text:
                self.page.headings.append(text)
            self._heading_tag = None
            self._heading_text = []


def parse_portal_html(html: str) -> ParsedPage:
    parser = _PortalParser()
    parser.feed(html)
    parser.close()
    return parser.page


def has_class(attrs: dict[str, str], expected: str) -> bool:
    return expected in attrs.get("class", "").split()
