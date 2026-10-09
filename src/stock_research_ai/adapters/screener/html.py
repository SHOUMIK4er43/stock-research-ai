"""Small HTML tree and table reader; intentionally no CSS selector engine.

The tree is built from untrusted network bytes, so every step is linear in the input
and the tree itself is bounded: building keeps per-parent sibling counters (no sibling
rescans) and an open-tag tally (no stack rescans for stray end tags), walks are
iterative (no recursion proportional to nesting), locators are derived on demand from
the parent chain (no per-node copy of every ancestor's path), and node count, nesting
depth, attributes per element, tag-name length and locator length are capped. The
locator cap is checked from a per-node length integer (an id resets it, like the
locator itself), so no locator string is built while parsing. Exceeding a cap raises
`HtmlLimitExceeded` - callers fail closed instead of reading a truncated tree, and a
provenance locator is never truncated."""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

from stock_research_ai.domain.provenance import SourceLink, SourceLocation

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())

#: Real Screener company pages hold ~2,200 elements, 15 deep, at most 7 attributes each
#: (fixtures and every replayed artifact). The caps leave ~45x, ~8x and ~9x headroom
#: while keeping the worst accepted tree to tens of MB and a bounded walk cost.
MAX_NODES = 100_000
#: Screener pages become one observation per table cell, each far larger than its markup,
#: so a page near MAX_NODES would cost ~1 GB of observations. Screener callers pass this
#: tighter cap (~9x the largest real page); document HTML keeps MAX_NODES.
MAX_SCREENER_NODES = 20_000
MAX_DEPTH = 128
MAX_ATTRIBUTES = 64
#: Every observation copies its cell's locator, so one long tag name or id would be
#: repeated per observation. The longest tag name in the fixtures, replayed artifacts and
#: stored documents is 12 characters (a custom element), the longest locator 160 and the
#: longest id-based locator base 43 ("#" + id). The caps leave ~5x and ~6x headroom.
MAX_TAG_NAME_LENGTH = 64
MAX_LOCATOR_LENGTH = 1024


class HtmlLimitExceeded(ValueError):
    """The page exceeds a structural bound; no partial tree is returned."""

    def __init__(self, limit: str) -> None:
        super().__init__(f"HTML structure limit exceeded: {limit}")
        self.limit = limit


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    _parent: "Node | None" = field(default=None, repr=False, compare=False)
    _step: str = field(default="", repr=False, compare=False)  # "/tag[n]" under _parent
    _length: int = field(default=len("document"), repr=False, compare=False)  # of locator

    @property
    def locator(self) -> str:
        """The root is "document"; an element with an id is "#id"; any other element is
        its parent's locator followed by one "/tag[n]" step. Never longer than
        MAX_LOCATOR_LENGTH: a longer one raises instead of being built or truncated."""
        steps: list[str] = []
        length = 0
        node: Node | None = self
        while node is not None:
            if "id" in node.attrs:
                identifier = node.attrs["id"]
                if len(identifier) >= MAX_LOCATOR_LENGTH:  # never copied, even once
                    raise HtmlLimitExceeded("locator")
                step, node = "#" + identifier, None
            elif node._parent is None:
                step, node = "document", None
            else:
                step, node = node._step, node._parent
            length += len(step)
            if length > MAX_LOCATOR_LENGTH:
                raise HtmlLimitExceeded("locator")
            steps.append(step)
        return "".join(reversed(steps))

    @property
    def text(self) -> str:
        return " ".join(self.raw_text.split())

    @property
    def raw_text(self) -> str:
        parts: list[str] = []
        stack = [iter(self.children)]
        while stack:
            for child in stack[-1]:
                if isinstance(child, str):
                    parts.append(child)
                else:
                    stack.append(iter(child.children))
                    break
            else:
                stack.pop()
        return "".join(parts)

    def descendants(self) -> Iterator["Node"]:
        """Every element below this one, in document order."""
        stack = [iter(self.children)]
        while stack:
            for child in stack[-1]:
                if isinstance(child, Node):
                    yield child
                    stack.append(iter(child.children))
                    break
            else:
                stack.pop()

    def all(self, tag: str | None = None) -> list["Node"]:
        return [n for n in self.descendants() if tag is None or n.tag == tag]

    def by_id(self, identifier: str) -> "Node | None":
        return next((n for n in self.descendants() if n.attrs.get("id") == identifier), None)

    def by_class(self, name: str) -> "Node | None":
        return next(
            (n for n in self.descendants() if name in n.attrs.get("class", "").split()), None
        )

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
    def __init__(self, text: str, max_nodes: int = MAX_NODES) -> None:
        super().__init__(convert_charrefs=True)
        self._max_nodes = max_nodes
        self.root = Node("document")
        self.stack = [self.root]
        # Parallel to `stack`: per open element, how many children of each tag it has.
        self._counts: list[Counter[str]] = [Counter()]
        self._open: Counter[str] = Counter()  # tags currently on `stack`
        self._nodes = 0
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._nodes += 1
        if self._nodes > self._max_nodes:
            raise HtmlLimitExceeded("nodes")
        if len(tag) > MAX_TAG_NAME_LENGTH:
            raise HtmlLimitExceeded("tag_name")
        if len(attrs) > MAX_ATTRIBUTES:
            raise HtmlLimitExceeded("attributes")
        parent = self.stack[-1]
        counts = self._counts[-1]
        attributes = {k: v or "" for k, v in attrs}
        step = f"/{tag}[{counts[tag] + 1}]"
        # Same rule as Node.locator, as one integer: an id restarts the locator.
        length = 1 + len(attributes["id"]) if "id" in attributes else parent._length + len(step)
        if length > MAX_LOCATOR_LENGTH:
            raise HtmlLimitExceeded("locator")
        counts[tag] += 1
        node = Node(tag, attributes, _parent=parent, _step=step, _length=length)
        parent.children.append(node)
        if tag not in VOID:
            if len(self.stack) > MAX_DEPTH:
                raise HtmlLimitExceeded("depth")
            self.stack.append(node)
            self._counts.append(Counter())
            self._open[tag] += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self._open[tag]:
            return  # stray end tag: nothing to close (and no stack scan)
        while True:  # each popped element was pushed once: amortised O(1)
            node = self.stack.pop()
            self._counts.pop()
            self._open[node.tag] -= 1
            if node.tag == tag:
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
