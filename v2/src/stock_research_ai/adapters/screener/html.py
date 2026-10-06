"""Small HTML tree and table reader; intentionally no CSS selector engine."""

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from stock_research_ai.domain.provenance import SourceLink, SourceLocation

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    locator: str = "document"

    @property
    def text(self) -> str:
        return " ".join(self.raw_text.split())

    @property
    def raw_text(self) -> str:
        return "".join(c if isinstance(c, str) else c.raw_text for c in self.children)

    def all(self, tag: str | None = None) -> list["Node"]:
        result: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    result.append(child)
                result.extend(child.all(tag))
        return result

    def by_id(self, identifier: str) -> "Node | None":
        return next((n for n in self.all() if n.attrs.get("id") == identifier), None)

    def by_class(self, name: str) -> "Node | None":
        return next((n for n in self.all() if name in n.attrs.get("class", "").split()), None)

    def location(self, section: str) -> SourceLocation:
        return SourceLocation(section=section, locator=self.locator)

    def link(self, final_url: str, section: str) -> SourceLink:
        href = self.attrs["href"]
        return SourceLink(
            original_href=href,
            resolved_url=urljoin(final_url, href),
            source_location=self.location(section),
        )


class Page(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.stack[-1]
        index = 1 + sum(isinstance(c, Node) and c.tag == tag for c in parent.children)
        attributes = {k: v or "" for k, v in attrs}
        locator = f"{parent.locator}/{tag}[{index}]"
        if "id" in attributes:
            locator = f"#{attributes['id']}"
        node = Node(tag, attributes, locator=locator)
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


def rows(table: Node) -> list[list[Node]]:
    return [
        [c for c in row.children if isinstance(c, Node) and c.tag in {"th", "td"}]
        for row in table.all("tr")
    ]


def data_tables(section: Node) -> list[Node]:
    return [t for t in section.all("table") if "data-table" in t.attrs.get("class", "").split()]
