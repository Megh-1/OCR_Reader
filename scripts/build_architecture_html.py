"""
Regenerate architecture.html from architecture.md.

architecture.md is the source of truth. The HTML is a reading copy: the page shell (fonts, CSS, the bundled Mermaid build and the
script that renders the diagrams and wraps tables) is kept exactly as it already is in architecture.html, and only the contents table
in the sidebar and the article are rebuilt from the Markdown. Run it again after editing the Markdown.

Needs markdown-it-py (`pip install markdown-it-py`), which the application itself does not use.

    python scripts/build_architecture_html.py [--md architecture.md] [--html architecture.html]
"""

from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

from markdown_it import MarkdownIt

SIDEBAR_START = "<aside>"
ARTICLE_END = "</article></main></div>"
FIRST_SCRIPT = "<script>"


def slugify(text: str, seen: dict[str, int]) -> str:
    """GitHub-style heading id: lower case, punctuation and emoji dropped, spaces to hyphens, repeats numbered."""
    slug = re.sub(r"[^\w\- ]", "", text.lower()).strip().replace(" ", "-")
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count}"


def plain(inline_token) -> str:
    return "".join(child.content for child in inline_token.children or [] if child.type in ("text", "code_inline"))


def render(markdown: str) -> tuple[str, list[tuple[int, str, str]]]:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
    tokens = md.parse(markdown)
    seen: dict[str, int] = {}
    headings: list[tuple[int, str, str]] = []  # (level, id, label html)

    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[i + 1]
            ident = slugify(plain(inline), seen)
            token.attrSet("id", ident)
            headings.append((int(token.tag[1]), ident, md.renderer.renderInline(inline.children or [], md.options, {})))

    default_fence = md.renderer.rules["fence"]

    def fence(self, tokens, idx, options, env):
        token = tokens[idx]
        if token.info.strip().split(" ")[0] == "mermaid":
            return f'<figure class="diagram"><pre class="mermaid">\n{html.escape(token.content)}</pre></figure>\n'
        return default_fence(tokens, idx, options, env)

    md.add_render_rule("fence", fence)
    return md.renderer.render(tokens, md.options, {}), headings


def sidebar(headings: list[tuple[int, str, str]]) -> str:
    title = next((label for level, _, label in headings if level == 1), "")
    out = [f'<aside><p class="t">{title}</p><ul>']
    open_item = False
    nested = False
    for level, ident, label in headings:
        if level == 1:
            continue
        if level == 2:
            if nested:
                out.append("</ul>")
                nested = False
            if open_item:
                out.append("</li>")
            out.append(f'<li><a href="#{ident}">{label}</a>')
            open_item = True
        elif level == 3:
            if not nested:
                out.append("<ul>")
                nested = True
            out.append(f'<li><a href="#{ident}">{label}</a></li>')
    if nested:
        out.append("</ul>")
    if open_item:
        out.append("</li>")
    out.append("</ul></aside>\n")
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--md", default="architecture.md")
    parser.add_argument("--html", default="architecture.html")
    args = parser.parse_args()

    md_path, html_path = Path(args.md), Path(args.html)
    shell = html_path.read_text(encoding="utf-8")
    head_end = shell.index(SIDEBAR_START)
    tail_start = shell.index(ARTICLE_END) + len(ARTICLE_END)
    assert shell.index(FIRST_SCRIPT, tail_start) >= tail_start, "the page's scripts must follow the article"

    body, headings = render(md_path.read_text(encoding="utf-8"))
    page = shell[:head_end] + sidebar(headings) + "<main><article>" + body + shell[tail_start - len(ARTICLE_END):]
    html_path.write_text(page, encoding="utf-8", newline="\n")
    diagrams = body.count('class="mermaid"')
    print(f"{html_path}: {len(page):,} bytes, {len(headings)} headings, {diagrams} diagrams")


if __name__ == "__main__":
    main()
