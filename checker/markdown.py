"""
Markdown — a deliberate subset, rendered server-side.

There is no markdown parser in the standard library and this project does not
take dependencies, so this is written here. That is a smaller job than it
sounds, because we do not want all of markdown: we want the twelve constructs
estate documentation actually uses, and a hard refusal on everything else.

The refusal is the point. **Raw HTML is escaped, never rendered**, which is
what makes the rendered output safe to cache in SQLite and serve back without
a sanitiser. There is no sanitiser to get wrong because nothing dangerous can
enter. A device-supplied printer string or a collector payload pasted into a
record is text, always.

Supported: ATX headings (## to ####), paragraphs, **bold**, *italic*, `code`,
fenced code blocks, unordered and ordered lists with one level of nesting,
pipe tables, block quotes, horizontal rules, links, local images, and two
house extensions:

    [[HST.PEP]]        a record link, checked against the catalogue
    > [!warn] text     a callout — warn, danger, note

Deliberately NOT supported: raw HTML, footnotes, definition lists, nested
block quotes, setext headings, reference-style links, inline styling, and
images from anywhere but the repository.

    html, links = render("## Title\\n\\nSee [[HST.PEP]].", resolve=cat.exists)

`links` is every record link found, as (shelf, resolved) pairs, which is what
makes a broken cross-reference a detectable condition rather than a dead chip
nobody notices.
"""

import re

# Headings start at h3: the page template already owns h1 (the site) and h2
# (the record title), so a document heading must not compete with either.
HEADING_BASE = 2

CALLOUTS = {"warn": "warn", "danger": "danger", "note": "note", "info": "note"}

_FENCE = re.compile(r"^```\s*([A-Za-z0-9_+-]*)\s*$")
_HEADING = re.compile(r"^(#{2,4})\s+(.*)$")
_RULE = re.compile(r"^-{3,}\s*$")
_CALLOUT = re.compile(r"^>\s*\[!([A-Za-z]+)\]\s*(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_UL = re.compile(r"^(\s*)[-*]\s+(.*)$")
_OL = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|?\s*$")

_CODE_SPAN = re.compile(r"`([^`]+)`")
_RECORD = re.compile(r"\[\[([^\]\n]+)\]\]")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)\)")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(\S(?:[^*]*\S)?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(\S(?:[^*]*\S)?)\*(?!\*)")

_SAFE_MEDIA = re.compile(r"^[A-Za-z0-9._/-]+$")
_SAFE_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


def esc(text):
    """HTML-escape. Applied before anything else, so markup in the source is
    shown as text rather than rendered."""
    return (str("" if text is None else text)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def split_front_matter(text):
    """Return (front_matter, body) for a document fenced with +++.

    TOML rather than YAML, because the project already speaks TOML and 3.11
    parses it in the standard library. A document with no front matter is not
    an error here — the caller decides whether that is one.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("+++"):
        return "", text
    end = text.find("\n+++", 3)
    if end < 0:
        return "", text
    fm = text[text.find("\n", 3) + 1:end]
    rest = text[end + 4:]
    return fm, rest.lstrip("\n")


class _Inline:
    """Inline rendering, with code spans held out of the way.

    Order matters: escape, then lift code spans into placeholders so nothing
    rewrites their contents, then images, record links, links, bold, italic,
    and finally put the code spans back.
    """

    def __init__(self, resolve, media_prefix, links, link_prefix):
        self.resolve = resolve
        self.media_prefix = media_prefix
        self.links = links
        self.link_prefix = link_prefix

    def __call__(self, text):
        held = []

        def hold(m):
            held.append(m.group(1))
            return "\x00%d\x00" % (len(held) - 1)

        out = _CODE_SPAN.sub(hold, esc(text))
        out = _IMAGE.sub(self._image, out)
        out = _RECORD.sub(self._record, out)
        out = _LINK.sub(self._link, out)
        out = _BOLD.sub(r"<strong>\1</strong>", out)
        out = _ITALIC.sub(r"<em>\1</em>", out)
        return re.sub(r"\x00(\d+)\x00",
                      lambda m: f"<code>{held[int(m.group(1))]}</code>", out)

    def _record(self, m):
        shelf = m.group(1).strip()
        ok = bool(self.resolve(shelf)) if self.resolve else False
        self.links.append((shelf, ok))
        if ok:
            return (f'<a class="sm" href="{self.link_prefix}'
                    f'{shelf_slug(shelf)}">{esc(shelf)}</a>')
        return (f'<span class="sm q" title="no such record">{esc(shelf)}</span>')

    def _link(self, m):
        text, url = m.group(1), m.group(2)
        if not url.startswith(("http://", "https://", "/citadel/")):
            return text                       # not a scheme we will follow
        return f'<a href="{esc(url)}" rel="noopener">{text}</a>'

    def _image(self, m):
        alt, src = m.group(1), m.group(2)
        # Repository-local files only. No remote images: a record must render
        # the same with no internet, and an external URL is a tracking pixel
        # waiting to happen.
        if not _SAFE_MEDIA.match(src) or ".." in src:
            return esc(f"![{alt}]({src})")
        if not src.lower().endswith(_SAFE_IMG_EXT):
            return esc(f"![{alt}]({src})")
        return f'<img src="{self.media_prefix}{esc(src)}" alt="{alt}" loading="lazy">'


def shelf_slug(shelf):
    """HST.PEP -> hst-pep. Used for URLs; the shelf mark itself stays as it is
    written, because that is what people say out loud."""
    return re.sub(r"[^a-z0-9]+", "-", str(shelf).lower()).strip("-")


def render(src, resolve=None, media_prefix="", link_prefix="/citadel/r/"):
    """Render the subset. Returns (html, links).

    `resolve(shelf)` is asked whether a record link points at something that
    exists. Pass None and every record link renders as unresolved, which is
    the honest answer when there is no catalogue to ask.

    `link_prefix` is where a record link points. The portal wants
    `/citadel/r/`; the offline manual wants `#`, because a link to a host that
    is not running is worse than no link at all.
    """
    links = []
    inline = _Inline(resolve, media_prefix, links, link_prefix)
    lines = (src or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = lines.replace("\x00", "").split("\n")
    out = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = _FENCE.match(line)
        if fence:
            lang, i = fence.group(1), i + 1
            buf = []
            while i < n and not _FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1                                     # consume closing fence
            if lang.lower() == "flow":
                # A diagram, not code. If it will not parse it falls
                # through to a code block rather than being guessed at:
                # a wrong diagram is worse than a visible one you can
                # still read.
                svg = _flow("\n".join(buf))
                if svg:
                    out.append(svg)
                    continue
            cls = f' class="lang-{esc(lang)}"' if lang else ""
            out.append("<pre><code{}>{}</code></pre>".format(cls, esc("\n".join(buf))))
            continue

        head = _HEADING.match(line)
        if head:
            level = HEADING_BASE + len(head.group(1)) - 1
            out.append("<h%d>%s</h%d>" % (level, inline(head.group(2).strip()),
                                          level))
            i += 1
            continue

        if _RULE.match(line):
            out.append("<hr>")
            i += 1
            continue

        call = _CALLOUT.match(line)
        if call:
            kind = CALLOUTS.get(call.group(1).lower(), "note")
            buf = [call.group(2)]
            i += 1
            while i < n and _QUOTE.match(lines[i]):
                buf.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append('<div class="callout {}"><b>{}</b>{}</div>'.format(kind, esc(kind), inline(" ".join(buf).strip())))
            continue

        if _QUOTE.match(line):
            buf = []
            while i < n and _QUOTE.match(lines[i]):
                buf.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            out.append("<blockquote>{}</blockquote>".format(inline(" ".join(buf).strip())))
            continue

        if _UL.match(line) or _OL.match(line):
            html, i = _list(lines, i, inline)
            out.append(html)
            continue

        if line.startswith("|"):
            html, i = _table(lines, i, inline)
            if html:
                out.append(html)
                continue

        # Always take the current line, even if it looks like a block start:
        # we only reach here because no block handler claimed it (a row of
        # pipes with no separator, say). Taking it unconditionally is what
        # guarantees the cursor moves and the loop terminates.
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not _block_start(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>{}</p>".format(inline(" ".join(buf))))

    return "\n".join(out), links


def _block_start(line):
    return bool(_HEADING.match(line) or _FENCE.match(line) or _RULE.match(line)
                or _QUOTE.match(line) or _UL.match(line) or _OL.match(line)
                or line.startswith("|"))


def _list(lines, i, inline):
    """One list, with at most one level of nesting. Deeper indentation is
    folded into its parent item rather than silently dropped."""
    n = len(lines)
    ordered = bool(_OL.match(lines[i]))
    pat = _OL if ordered else _UL
    base = len(pat.match(lines[i]).group(1))
    items = []

    while i < n:
        m = pat.match(lines[i])
        if not m:
            break                   # a list of the other kind ends this one
        indent = len(m.group(1))
        if indent < base:
            break
        if indent > base:                       # nested: attach to the last
            sub, i = _list(lines, i, inline)    # item rather than start over
            if items:
                items[-1] += sub
            continue
        text = m.group(2)
        i += 1
        # a continuation line is indented and is not itself a list item
        while (i < n and lines[i].strip() and not _block_start(lines[i])
               and lines[i][:1] in " \t"):
            text += " " + lines[i].strip()
            i += 1
        items.append(f"<li>{inline(text)}")

    tag = "ol" if ordered else "ul"
    return "<{}>{}</{}>".format(tag, "".join(x + "</li>" for x in items), tag), i


def _table(lines, i, inline):
    """A pipe table. Requires the separator row — without it this is just a
    paragraph that happens to contain pipes, and treating it as a table is how
    you mangle a sentence about `a | b`."""
    n = len(lines)
    start = i
    rows = []
    while i < n and lines[i].startswith("|"):
        rows.append(lines[i])
        i += 1
    if len(rows) < 2 or not _TABLE_SEP.match(rows[1]):
        return "", start

    def cells(row):
        return [c.strip() for c in row.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]]
    out = ['<div class="tw"><table><thead><tr>']
    out += [f"<th>{inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        out += [f"<td>{inline(c)}</td>" for c in row]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out), i


def plain(src):
    """The document as searchable text: no markup, no front matter, record
    links reduced to their shelf marks. This is what goes into the FTS index,
    so a search for a word finds it whether or not it was inside a link."""
    _, body = split_front_matter(src or "")
    body = re.sub(r"```.*?```", " ", body, flags=re.S)
    body = _RECORD.sub(r"\1", body)
    body = _IMAGE.sub(" ", body)
    body = _LINK.sub(r"\1", body)
    body = re.sub(r"[#>*`|_-]+", " ", body)
    return re.sub(r"\s+", " ", body).strip()


# --------------------------------------------------------------- diagrams

_FLOW_NODE = re.compile(r"^(?P<label>[^|]+?)(?:\s*\|\s*(?P<sub>[^|]*))?"
                        r"(?:\s*\|\s*(?P<style>accent|warn|plain))?\s*$")

FLOW_STYLE = {
    "plain":  ("bx", "var(--ink)", "var(--ink-3)"),
    "accent": ("bx-a", "var(--acc)", "var(--acc)"),
    "warn":   ("bx-r", "var(--red)", "var(--red)"),
}


def _flow_node(text):
    m = _FLOW_NODE.match(text.strip())
    if not m:
        return None
    return (m.group("label").strip(),
            (m.group("sub") or "").strip(),
            m.group("style") or "plain")


def _flow(src):
    """A ```flow block into an animated SVG.

    The source stays plain text, so a change to the architecture shows up in a
    diff as the line that moved. An image would change silently, which is the
    whole reason this is a small language rather than a PNG.

    Grammar, deliberately tiny:

        title: Where WinMan runs      an optional caption
        Label | sub | accent          a node in the main chain
        -> label on the arrow         an arrow to the next node
        => Label | sub                a branch off the previous node

    Anything it cannot parse is left as a code block rather than guessed at.
    """
    title, chain, branches = "", [], {}
    for raw in (src or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip()
            continue
        if line.startswith("=>"):
            node = _flow_node(line[2:])
            if node is None or not chain:
                return None
            # key by position among the NODES, not among the raw lines:
            # the render loop walks nodes, so an arrow in between would
            # shift the index and the branch would vanish without a word
            here = sum(1 for x in chain if x[0] != "__arrow__") - 1
            branches.setdefault(here, []).append(node)
            continue
        if line.startswith("->"):
            if not chain:
                return None
            chain.append(("__arrow__", line[2:].strip(), ""))
            continue
        node = _flow_node(line)
        if node is None:
            return None
        chain.append(node)

    nodes = [n for n in chain if n[0] != "__arrow__"]
    if len(nodes) < 1:
        return None

    labels, seq = [], []
    for item in chain:
        if item[0] == "__arrow__":
            labels.append(item[1])
        else:
            if len(labels) < len(seq):
                labels.append("")
            seq.append(item)

    BW, BH, GAP = 150, 54, 56
    fan = max((len(v) for v in branches.values()), default=0)
    rows = max(1, fan)
    top = 26 if title else 8
    cy = top + (BH // 2)
    height = top + BH + 16 + (rows * (BH + 10) if fan else 0)
    width = len(seq) * BW + max(0, len(seq) - 1) * GAP + 24
    if fan:
        width += BW + GAP

    out = ['<svg class="flow" viewBox="0 0 %d %d" role="img" '
           'preserveAspectRatio="xMinYMin meet" aria-label="%s">'
           % (width, height, esc(title or "architecture diagram"))]
    out.append('<defs><marker id="fa" viewBox="0 0 10 10" refX="9" refY="5" '
               'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
               '<path d="M0 0 L10 5 L0 10 z" fill="var(--acc)"/></marker>'
               '<marker id="fr" viewBox="0 0 10 10" refX="9" refY="5" '
               'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
               '<path d="M0 0 L10 5 L0 10 z" fill="var(--red)"/></marker>'
               "</defs>")
    if title:
        out.append(f'<text class="fg" x="12" y="16">{esc(title)}</text>')

    def box(x, y, label, sub, style):
        cls, fill, subfill = FLOW_STYLE.get(style, FLOW_STYLE["plain"])
        s = ['<rect class="%s" x="%d" y="%d" width="%d" height="%d"/>'
             % (cls, x, y, BW, BH)]
        s.append('<text class="ft" x="%d" y="%d" fill="%s">%s</text>'
                 % (x + 12, y + 22, fill, esc(label[:26])))
        if sub:
            s.append('<text class="fs" x="%d" y="%d" fill="%s">%s</text>'
                     % (x + 12, y + 39, subfill, esc(sub[:30])))
        return "".join(s)

    def arrow(x1, y1, x2, y2, red=False, dur=2.2):
        d = "M%d %d L%d %d" % (x1, y1, x2, y2)
        col = "var(--red)" if red else "var(--acc)"
        mk = "fr" if red else "fa"
        s = [f'<path class="fl" d="{d}" stroke="{col}" marker-end="url(#{mk})"/>']
        s.append(f'<circle r="2.6" fill="{col}"><animateMotion dur="{dur:.1f}s" '
                 f'repeatCount="indefinite" path="{d}"/></circle>')
        return "".join(s)

    for i, (label, sub, style) in enumerate(seq):
        x = 12 + i * (BW + GAP)
        out.append(box(x, top, label, sub, style))
        if i:
            out.append(arrow(x - GAP, cy, x - 4, cy, dur=1.8 + i * 0.2))
            lab = labels[i - 1] if i - 1 < len(labels) else ""
            if lab:
                out.append('<text class="fa" x="%d" y="%d">%s</text>'
                           % (x - GAP + 2, top - 6, esc(lab[:26])))
        for j, (bl, bs, bst) in enumerate(branches.get(i, [])):
            bx = x + BW + GAP
            by = top + BH + 16 + j * (BH + 10)
            out.append(box(bx, by, bl, bs, bst))
            out.append(arrow(x + BW // 2, top + BH + 2, bx - 4, by + BH // 2,
                             red=(bst == "warn"), dur=2.4 + j * 0.3))

    out.append("</svg>")
    return '<div class="flowwrap">{}</div>'.format("".join(out))
