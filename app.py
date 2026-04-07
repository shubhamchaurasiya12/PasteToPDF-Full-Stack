"""
app.py — Flask backend for PasteToDoc
"""

import io
import time
import logging
from collections import defaultdict
from threading import Lock

from flask import (
    Flask,
    request,
    send_file,
    render_template,
    jsonify,
)
from renderer import generate_pdf

# ─────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB max request body

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pastetodoc")

# ─────────────────────────────────────────────────────────────
# Simple in-memory rate limiter
# ─────────────────────────────────────────────────────────────
RATE_LIMIT  = 10
RATE_WINDOW = 60
_rate_store = defaultdict(list)
_rate_lock  = Lock()


def _get_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _check_rate_limit(ip):
    now = time.monotonic()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_WINDOW]
        if len(_rate_store[ip]) >= RATE_LIMIT:
            return False
        _rate_store[ip].append(now)
        return True


# ─────────────────────────────────────────────────────────────
# Security headers
# ─────────────────────────────────────────────────────────────
@app.after_request
def add_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-XSS-Protection"]       = "1; mode=block"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    if request.path != "/api/preview":
        resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src  'self' 'unsafe-inline' https://fonts.googleapis.com "
                   "https://cdnjs.cloudflare.com; "
        "font-src   'self' https://fonts.gstatic.com "
                   "https://cdnjs.cloudflare.com data:; "
        "img-src    'self' data:; "
        "connect-src 'self';"
    )
    return resp


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/preview", methods=["POST"])
def preview():
    """Return server-rendered HTML for the live preview iframe."""
    data = request.get_json(silent=True)
    if not data:
        return "", 204

    text      = (data.get("text") or "").strip()
    title     = (data.get("title") or "")[:200].strip()
    meta      = (data.get("meta") or "")[:200].strip()
    page_size = (data.get("pageSize") or "A4").strip()

    if not text:
        return "", 204

    from renderer import (
        extract_math, markdown_to_html, restore_math,
        wrap_tables, build_document_html, MAX_INPUT_CHARS,
    )

    if len(text) > MAX_INPUT_CHARS:
        return jsonify({"error": "Input too large."}), 422

    try:
        safe_text, blocks = extract_math(text)
        content_html      = markdown_to_html(safe_text)
        content_html      = restore_math(content_html, blocks)
        content_html      = wrap_tables(content_html)
        full_html         = build_document_html(content_html, title, meta, page_size)
        return full_html, 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as exc:
        log.exception("Preview render failed")
        return (
            f"<p style='color:red;font-family:sans-serif;padding:20px'>"
            f"Preview error: {exc}</p>"
        ), 500


@app.route("/api/render", methods=["POST"])
def render():
    ip = _get_ip()
    if not _check_rate_limit(ip):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON body."}), 400

    text      = (data.get("text") or "").strip()
    title     = (data.get("title") or "")[:200].strip()
    meta      = (data.get("meta") or "")[:200].strip()
    page_size = (data.get("pageSize") or "A4").strip()

    if not text:
        return jsonify({"error": "No text provided."}), 400

    log.info("Render request from %s — %d chars", ip, len(text))

    try:
        pdf_bytes = generate_pdf(text, title, meta, page_size)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except RuntimeError as exc:
        log.exception("Render failed")
        return jsonify({"error": str(exc)}), 500

    filename = (title[:60].strip() or "document").replace(" ", "_") + ".pdf"

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)