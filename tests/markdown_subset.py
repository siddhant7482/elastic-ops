#!/usr/bin/env python3
"""
The markdown subset — what it renders, and what it refuses.

The refusals matter more than the features. Rendered HTML is cached in SQLite
and served back without a sanitiser, which is only safe because nothing
dangerous can get in. Most of this file is about that.

Run it from the repository root:

    python3 tests/markdown_subset.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "checker"))

import markdown as md                                            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{('  — ' + str(extra)) if extra and not cond else ''}")


def html(src, **kw):
    return md.render(src, **kw)[0]


KNOWN = {"HST.PEP", "RUN.CFP", "REG.CRED"}
resolve = KNOWN.__contains__


print("\nblocks")
check("h2 becomes h3, not h1", html("## Title") == "<h3>Title</h3>")
check("h3 and h4 step down", html("### A\n\n#### B") == "<h4>A</h4>\n<h5>B</h5>")
check("a single # is not a heading", "<h" not in html("# Title"))
check("paragraph joins wrapped lines",
      html("one\ntwo") == "<p>one two</p>")
check("blank line separates paragraphs",
      html("one\n\ntwo") == "<p>one</p>\n<p>two</p>")
check("horizontal rule", "<hr>" in html("a\n\n---\n\nb"))
check("block quote", "<blockquote>q</blockquote>" == html("> q"))
check("fenced code keeps its language",
      'class="lang-sh"' in html("```sh\nls\n```"))
check("fenced code is not reflowed",
      "<code>a\n  b</code>" in html("```\na\n  b\n```"))

print("\nlists")
check("unordered", html("- a\n- b") == "<ul><li>a</li><li>b</li></ul>")
check("ordered", html("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>")
check("a continuation line folds into its item",
      html("- a\n  still a") == "<ul><li>a still a</li></ul>")
check("one level of nesting", html("- a\n  - b") ==
      "<ul><li>a<ul><li>b</li></ul></li></ul>")
check("list ends at a blank line",
      html("- a\n\npara") == "<ul><li>a</li></ul>\n<p>para</p>")

print("\ntables")
t = html("| a | b |\n| --- | --- |\n| 1 | 2 |")
check("header row", "<th>a</th><th>b</th>" in t)
check("body row", "<td>1</td><td>2</td>" in t)
check("scroll wrapper", 'class="tw"' in t)
check("pipes without a separator row stay a paragraph",
      html("| a | b |") == "<p>| a | b |</p>")
check("a sentence about `a | b` is not a table",
      "<table" not in html("use `a | b` for that"))

print("\ninline")
check("bold", html("**x**") == "<p><strong>x</strong></p>")
check("italic", html("*x*") == "<p><em>x</em></p>")
check("bold wins over italic", html("**x**") .count("<em>") == 0)
check("code span", html("`x`") == "<p><code>x</code></p>")
check("markup inside a code span is literal",
      html("`**x**`") == "<p><code>**x**</code></p>")
check("an asterisk in prose is not italic",
      "<em>" not in html("2 * 3 * 4"))

print("\nrecord links")
h, links = md.render("See [[HST.PEP]].", resolve=resolve)
check("resolved link is an anchor", '<a class="sm" href="/citadel/r/hst-pep"' in h)
check("resolved link is reported", links == [("HST.PEP", True)])
h, links = md.render("See [[GST.9999]].", resolve=resolve)
check("unresolved link is a dashed chip", 'class="sm q"' in h)
check("unresolved link is reported", links == [("GST.9999", False)])
check("the link target is configurable, for the offline manual",
      '<a class="sm" href="#hst-pep"' in html("[[HST.PEP]]", resolve=resolve,
                                               link_prefix="#"))
check("no resolver means nothing resolves",
      md.render("[[HST.PEP]]")[1] == [("HST.PEP", False)])
check("every link is reported, in order",
      md.render("[[HST.PEP]] [[X]] [[RUN.CFP]]", resolve=resolve)[1]
      == [("HST.PEP", True), ("X", False), ("RUN.CFP", True)])

print("\ncallouts")
c = html("> [!warn] mind this")
check("warn", 'class="callout warn"' in c and "mind this" in c)
check("danger", 'class="callout danger"' in html("> [!danger] no"))
check("note", 'class="callout note"' in html("> [!note] fyi"))
check("an unknown kind falls back to note",
      'class="callout note"' in html("> [!wat] hm"))
check("a callout spans its continuation lines",
      "line one line two" in html("> [!warn] line one\n> line two"))

print("\nrefusals — the reason the cache is safe to serve")
check("script tags are escaped",
      "&lt;script&gt;" in html("<script>alert(1)</script>")
      and "<script>" not in html("<script>alert(1)</script>"))
check("img onerror is escaped",
      "<img" not in html('<img src=x onerror=alert(1)>'))
check("raw html in a table cell is escaped",
      "<b>" not in html("| a |\n| --- |\n| <b>x</b> |"))
check("raw html in a heading is escaped",
      "<i>" not in html("## <i>x</i>"))
check("quotes are escaped so attributes cannot break out",
      "&quot;" in html('say "hi"'))
check("javascript: urls are dropped",
      "javascript:" not in html("[x](javascript:alert(1))"))
check("data: urls are dropped", "data:" not in html("[x](data:text/html,hi)"))
check("relative links are dropped", "<a" not in html("[x](../../etc/passwd)"))
check("http links survive", '<a href="http://x.test"' in html("[x](http://x.test)"))
check("remote images are refused",
      "<img" not in html("![a](https://evil.test/p.png)"))
check("path traversal in an image is refused",
      "<img" not in html("![a](../../secret.png)"))
check("a non-image extension is refused",
      "<img" not in html("![a](notes.md)"))
check("a repository image renders",
      '<img src="/m/d.png"' in html("![a](d.png)", media_prefix="/m/"))
check("NUL bytes cannot forge a code-span placeholder",
      "<code>" not in html("\x000\x00"))

print("\ntermination — every branch must advance the cursor")
# A row of pipes with no separator row once spun forever: the table handler
# declined it and the paragraph handler treated it as a block start, so
# nothing consumed the line. Any input that renders at all must terminate.
ADVERSARIAL = [
    "| a | b |", "|", "||", "|\n|\n|", "> ", ">", "```", "```\n", "---",
    "-", "- ", "1.", "1. ", "  - a", "\t- a", "#", "##", "## ", "[[", "]]",
    "[[]]", "`", "**", "*", "![](", "|---|", "|a|\n|a|", "> [!]", "> [!warn]",
    "- a\n  1. b\n- c", "1. a\n   - b\n2. c", "\x00", "\x00\x00", "a\x00b",
]
for s in ADVERSARIAL:
    try:
        md.render(s)
        md.plain(s)
        ok, why = True, ""
    except Exception as e:                                       # noqa: BLE001
        ok, why = False, repr(e)
    check(f"terminates and does not raise on {s!r}", ok, why)

print("\nfront matter")
fm, body = md.split_front_matter('+++\ntitle = "x"\n+++\n\n## Body\n')
check("front matter is lifted", fm.strip() == 'title = "x"')
check("body starts after the fence", body.strip() == "## Body")
check("no front matter is not an error",
      md.split_front_matter("## Body") == ("", "## Body"))
check("an unterminated fence is left alone",
      md.split_front_matter("+++\nbroken")[0] == "")
check("CRLF is normalised",
      md.split_front_matter('+++\r\na = 1\r\n+++\r\nx')[1].strip() == "x")

print("\nsearch text")
p = md.plain('+++\ntitle = "x"\n+++\n## Head\n\nSee [[HST.PEP]] and `code`.\n')
check("front matter is excluded", "title" not in p)
check("record links reduce to the shelf mark", "HST.PEP" in p)
check("headings keep their words", "Head" in p)
check("fenced code is excluded", "zz" not in md.plain("```\nzz\n```"))
check("no markup survives", "<" not in p and "[" not in p)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
