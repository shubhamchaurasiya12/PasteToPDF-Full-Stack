"""
renderer.py — Core PDF generation pipeline (server-side only).

Pipeline:
  1. extract_math()        – pull out all LaTeX before markdown touches it
  2. markdown_to_html()    – parse markdown → HTML
  3. restore_math()        – put placeholders back as data-math attrs
  4. wrap_tables()         – add table-wrapper divs
  5. build_document_html() – assemble full HTML page with styles + KaTeX CDN
  6. generate_pdf()        – Playwright headless-Chrome → PDF bytes
"""

import re
import html as html_lib
import markdown
from markdown.extensions.tables import TableExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.nl2br import Nl2BrExtension
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import atexit
from threading import Lock
from playwright.sync_api import sync_playwright, Browser

# ─────────────────────────────────────────────────────────────
# 1. Math extraction  (protect LaTeX from the markdown parser)
# ─────────────────────────────────────────────────────────────
PLACEHOLDER = "\x00MATH{}\x00"
PLACEHOLDER_RE = re.compile(r"\x00MATH(\d+)\x00")


def extract_math(text: str) -> tuple[str, list[dict]]:
    """
    Remove all math blocks from markdown source and replace with
    numbered placeholders so markdown never mangles underscores,
    asterisks, or backslashes inside equations.
    Returns (sanitised_text, list_of_blocks).
    """
    blocks: list[dict] = []

    def _placeholder(display: bool, math: str) -> str:
        blocks.append({"display": display, "math": math.strip()})
        return PLACEHOLDER.format(len(blocks) - 1)

    # Order matters: longest delimiters first

    # $$ … $$  (display)
    text = re.sub(
        r"\$\$([\s\S]*?)\$\$",
        lambda m: _placeholder(True, m.group(1)),
        text,
    )
    # \[ … \]  (display)
    text = re.sub(
        r"\\\[([\s\S]*?)\\\]",
        lambda m: _placeholder(True, m.group(1)),
        text,
    )
    # \( … \)  (inline)
    text = re.sub(
        r"\\\(([\s\S]*?)\\\)",
        lambda m: _placeholder(False, m.group(1)),
        text,
    )
    # $ … $  (inline) — skip lone dollar signs that look like prices
    def _maybe_inline(m: re.Match) -> str:
        math = m.group(1)
        if re.fullmatch(r"[\d,\.]+", math.strip()):  # $5.00 etc.
            return m.group(0)
        return _placeholder(False, math)

    text = re.sub(r"\$(?=[^\s$\n])([^$\n]+?)(?<=[^\s$])\$", _maybe_inline, text)

    return text, blocks


# ─────────────────────────────────────────────────────────────
# 2. Markdown → HTML
# ─────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    md = markdown.Markdown(
        extensions=[
            TableExtension(),
            FencedCodeExtension(),
            Nl2BrExtension(),
            "attr_list",
            "def_list",
        ]
    )
    return md.convert(text)


# ─────────────────────────────────────────────────────────────
# 3. Restore math placeholders as data-math attributes
# ─────────────────────────────────────────────────────────────

def restore_math(html: str, blocks: list[dict]) -> str:
    def _replacer(m: re.Match) -> str:
        b = blocks[int(m.group(1))]
        # Escape math for HTML attribute
        safe = html_lib.escape(b["math"], quote=True)
        if b["display"]:
            return f'<div class="math-display" data-math="{safe}" data-display="1"></div>'
        return f'<span class="math-inline" data-math="{safe}"></span>'

    return PLACEHOLDER_RE.sub(_replacer, html)


# ─────────────────────────────────────────────────────────────
# 4. Wrap tables for overflow / print safety
# ─────────────────────────────────────────────────────────────

def wrap_tables(html: str) -> str:
    html = re.sub(r"<table>", '<div class="table-wrapper"><table>', html)
    html = re.sub(r"</table>", "</table></div>", html)
    return html


# ─────────────────────────────────────────────────────────────
# 5. Assemble full HTML page
# ─────────────────────────────────────────────────────────────

DOCUMENT_CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=Lora:ital,wght@0,400;0,500;0,600;1,400;1,500&family=Fira+Code:wght@400;500&family=Outfit:wght@300;400;500;600;700&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Lora', Georgia, serif;
  font-size: 11pt;
  line-height: 1.75;
  color: #1a1a1e;
  background: white;
  padding: 0;
}

/* ── Title & meta ───────────────────────────── */
.doc-title {
  font-family: 'DM Serif Display', 'Georgia', serif;
  font-size: 24pt;
  font-weight: 400;
  color: #111118;
  line-height: 1.2;
  margin-bottom: 6px;
  break-inside: avoid;
  break-after: avoid;
}

.doc-meta {
  font-family: 'Outfit', sans-serif;
  font-size: 8.5pt;
  color: #6a6a7a;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin-bottom: 32px;
  padding-bottom: 18px;
  border-bottom: 1.5px solid #d0cbc0;
}

/* ── Headings ───────────────────────────────── */
h1 {
  font-family: 'DM Serif Display', Georgia, serif;
  font-size: 20pt;
  font-weight: 400;
  color: #111118;
  margin: 28px 0 10px;
  line-height: 1.25;
  break-after: avoid;
  break-inside: avoid;
}
h2 {
  font-family: 'DM Serif Display', Georgia, serif;
  font-size: 15pt;
  font-weight: 400;
  color: #111118;
  margin: 24px 0 8px;
  line-height: 1.3;
  break-after: avoid;
  break-inside: avoid;
}
h3 {
  font-family: 'Outfit', sans-serif;
  font-size: 11pt;
  font-weight: 700;
  color: #1a1a26;
  margin: 20px 0 6px;
  break-after: avoid;
  break-inside: avoid;
}
h4 {
  font-family: 'Outfit', sans-serif;
  font-size: 10pt;
  font-weight: 700;
  color: #2a2a3a;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin: 16px 0 4px;
  break-after: avoid;
  break-inside: avoid;
}

/* ── Body text ──────────────────────────────── */
p {
  margin: 0 0 12px;
  orphans: 3;
  widows: 3;
}

ul, ol {
  margin: 0 0 12px 0;
  padding-left: 26px;
}
li {
  margin-bottom: 4px;
  line-height: 1.65;
  break-inside: avoid;
}
li > ul, li > ol { margin: 4px 0; }

strong { font-weight: 600; color: #111118; }
em     { font-style: italic; }

a { color: #4a6cf7; text-decoration: underline; }

blockquote {
  margin: 12px 0 16px;
  padding: 10px 18px;
  border-left: 3px solid #e8b44b;
  background: rgba(232,180,75,0.06);
  color: #5a5a6a;
  font-style: italic;
  break-inside: avoid;
}

hr {
  border: none;
  border-top: 1.5px solid #d0cbc0;
  margin: 24px 0;
}

/* ── Code ───────────────────────────────────── */
code {
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 0.82em;
  background: #ede9df;
  color: #4a3060;
  padding: 1px 5px;
  border-radius: 3px;
  word-break: break-word;
}

pre {
  background: #1e1e2e;
  border-radius: 6px;
  padding: 14px 18px;
  margin: 12px 0 16px;
  border-left: 3px solid #7c8cf8;
  white-space: pre-wrap;
  word-break: break-all;
  word-wrap: break-word;
  break-inside: avoid;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
pre code {
  background: transparent;
  color: #cdd6f4;
  padding: 0;
  font-size: 0.78em;
  line-height: 1.6;
  border-radius: 0;
}

/* ── Tables ─────────────────────────────────── */
.table-wrapper {
  width: 100%;
  margin: 12px 0 18px;
  overflow: visible;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-family: 'Outfit', sans-serif;
  font-size: 9.5pt;
  table-layout: fixed;
  /* KEY: allow rows to split across pages, but not individual rows */
  break-inside: auto;
}

thead {
  display: table-header-group;   /* repeat header on every continuation page */
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}

tr {
  break-inside: avoid;           /* never cut a row in half */
  break-after: auto;
}

th {
  background: #1e1e2e;
  color: #e0e2f0;
  font-weight: 600;
  letter-spacing: 0.04em;
  font-size: 8.5pt;
  text-transform: uppercase;
  padding: 9px 12px;
  text-align: left;
  border: 1px solid #2e2e40;
  word-wrap: break-word;
  overflow-wrap: break-word;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}

td {
  padding: 8px 12px;
  border: 1px solid #d0cbc0;
  vertical-align: top;
  word-wrap: break-word;
  overflow-wrap: break-word;
  min-width: 0;
}

tbody tr:nth-child(even) td {
  background: #f7f7fb;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}

/* ── Math ───────────────────────────────────── */
.math-display {
  text-align: center;
  margin: 18px 0;
  overflow: visible;
  break-inside: avoid;
}

.math-inline {
  display: inline;
}

.math-error {
  color: #e06c75;
  font-family: 'Fira Code', monospace;
  font-size: 0.82em;
  background: rgba(224,108,117,0.1);
  padding: 2px 6px;
  border-radius: 3px;
}

/* ── KaTeX overrides ────────────────────────── */
.katex-display {
  overflow: visible !important;
  margin: 0 !important;
}
.katex { font-size: 1.05em; }
"""


def build_document_html(
    content_html: str,
    title: str = "",
    meta: str = "",
    page_size: str = "A4",
) -> str:
    title_block = f'<div class="doc-title">{html_lib.escape(title)}</div>' if title else ""
    meta_block  = f'<div class="doc-meta">{html_lib.escape(meta)}</div>'  if meta  else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
<style>
{DOCUMENT_CSS}
@page {{ size: {page_size}; margin: 2.2cm 2.5cm 2.5cm 2.5cm; }}
</style>
</head>
<body>
{title_block}
{meta_block}
<div id="content">{content_html}</div>
<script>
// Render all math elements injected by the server
document.querySelectorAll('[data-math]').forEach(function(el) {{
    var math    = el.getAttribute('data-math');
    var display = el.dataset.display === '1';
    try {{
        katex.render(math, el, {{
            displayMode:  display,
            throwOnError: false,
            trust:        true,
            strict:       false
        }});
    }} catch (e) {{
        el.innerHTML = '<span class="math-error">' + math + '</span>';
    }}
}});
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────
# Persistent browser singleton (thread‑safe)
# ─────────────────────────────────────────────────────────────
_playwright = None
_browser = None
_browser_lock = Lock()

def _get_browser() -> Browser:
    global _playwright, _browser
    with _browser_lock:
        if _browser is None:
            _playwright = sync_playwright().start()
            _browser = _playwright.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            # Register cleanup on interpreter exit
            atexit.register(_close_browser)
        return _browser

def _close_browser():
    global _playwright, _browser
    with _browser_lock:
        if _browser:
            _browser.close()
            _browser = None
        if _playwright:
            _playwright.stop()
            _playwright = None


# ─────────────────────────────────────────────────────────────
# 6. Generate PDF  (Playwright headless Chrome)
# ─────────────────────────────────────────────────────────────

MAX_INPUT_CHARS = 100_000   # ~25 000 words — generous but bounded
PLAYWRIGHT_TIMEOUT = 30_000  # ms


def generate_pdf(
    markdown_text: str,
    title: str = "",
    meta: str = "",
    page_size: str = "A4",
) -> bytes:
    """
    Full pipeline: markdown text → PDF bytes.
    Reuses a single persistent browser.
    """
    if not markdown_text or not markdown_text.strip():
        raise ValueError("No content provided.")
    if len(markdown_text) > MAX_INPUT_CHARS:
        raise ValueError(f"Input too large ({len(markdown_text):,} chars). Maximum is {MAX_INPUT_CHARS:,} chars.")

    page_size = page_size.upper()
    if page_size not in {"A4", "LETTER", "A3", "LEGAL"}:
        page_size = "A4"

    # Pipeline
    safe_text, blocks = extract_math(markdown_text)
    content_html = markdown_to_html(safe_text)
    content_html = restore_math(content_html, blocks)
    content_html = wrap_tables(content_html)
    full_html = build_document_html(content_html, title, meta, page_size)

    # Use persistent browser
    browser = _get_browser()
    page = browser.new_page()
    try:
        page.set_content(full_html, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT)
        # Wait extra time for KaTeX to finish (adjust as needed)
        page.wait_for_timeout(800)
        pdf_bytes = page.pdf(
            format=page_size,
            margin={
                "top": "2.2cm",
                "right": "2.5cm",
                "bottom": "2.5cm",
                "left": "2.5cm",
            },
            print_background=True,
            prefer_css_page_size=False,
        )
    except PlaywrightTimeout as exc:
        raise RuntimeError("PDF render timed out. Try a shorter document.") from exc
    except Exception as exc:
        raise RuntimeError(f"PDF render failed: {exc}") from exc
    finally:
        page.close()

    return pdf_bytes