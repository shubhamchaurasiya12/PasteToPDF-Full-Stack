# PasteToDoc — AI Response to PDF

Paste any ChatGPT / Claude / Gemini response and download a beautifully formatted PDF.  
All rendering logic runs **server-side** — clients only get a thin UI and the finished PDF.

## Architecture

```
Browser (thin UI)
    │  POST /api/render  {text, title, meta, pageSize}
    ▼
Flask  (app.py)
    │  validate + rate-limit
    ▼
renderer.py  ◄── never sent to client
    │  1. extract_math()        protect LaTeX from markdown parser
    │  2. markdown_to_html()    Python-Markdown → HTML
    │  3. restore_math()        inject data-math attributes
    │  4. wrap_tables()         add overflow-safe wrappers
    │  5. build_document_html() full page with KaTeX CDN + CSS
    │  6. Playwright            headless Chrome → PDF bytes
    ▼
Browser  ◄── receives only the finished .pdf file
```

## Quick Start

```bash
# 1. Clone / unzip the project
cd ai-to-pdf

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Install Playwright's headless Chromium (one-time)
playwright install chromium

# 5. Run the server
python app.py
```

Open **http://localhost:5000** in your browser.

## Production Deployment (Gunicorn + Nginx)

```bash
pip install gunicorn

# Run with 2 workers (each uses one Playwright instance at a time)
gunicorn -w 2 -b 0.0.0.0:5000 app:app
```

Nginx config snippet:

```nginx
location / {
    proxy_pass         http://127.0.0.1:5000;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 60s;
}
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MAX_INPUT_CHARS` | 100 000 | Max characters per request |
| `RATE_LIMIT` | 10 | Requests per window per IP |
| `RATE_WINDOW` | 60 s | Rate limit window |
| `PLAYWRIGHT_TIMEOUT` | 30 000 ms | Max render time |

Edit `renderer.py` to change limits.

## Security Model

- `renderer.py` is **never served to the browser** — it runs only on the server.
- The browser receives: `index.html` (UI shell) + the finished `.pdf` blob.
- Security headers on every response: `X-Frame-Options`, `CSP`, `X-Content-Type-Options`.
- Input size cap (1 MB body, 100 k chars content).
- In-memory rate limiter (10 req / 60 s per IP).

## Supported Markdown Features

- Headings (`#` through `######`)
- Bold, italic, strikethrough
- Ordered and unordered lists (nested)
- Inline code and fenced code blocks (with language tag)
- Tables (rows split cleanly across pages; header repeats)
- Block quotes
- Horizontal rules
- Inline math: `$x^2$` or `\(x^2\)`
- Display math: `$$\int_a^b$$` or `\[...\]`
- Links
