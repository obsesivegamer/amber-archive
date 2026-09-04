"""Build a readable article page that matches a typical archive.today news snapshot."""

from __future__ import annotations

from html import escape
import re

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z“\"])")


def _paragraphs(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(parts) == 1 and len(parts[0]) > 280:
        sentences = [s.strip() for s in _SENTENCE.split(parts[0]) if s.strip()]
        grouped: list[str] = []
        buf: list[str] = []
        for sentence in sentences:
            buf.append(sentence)
            if len(buf) == 2:
                grouped.append(" ".join(buf))
                buf = []
        if buf:
            grouped.append(" ".join(buf))
        return grouped or parts
    return parts


def _lead_html(paragraph: str) -> str:
    words = paragraph.split()
    n = 4 if len(words) > 4 else max(1, min(3, len(words)))
    lead = escape(" ".join(words[:n]))
    rest = escape(" ".join(words[n:]))
    if rest:
        return f"<p><strong>{lead}</strong> {rest}</p>"
    return f"<p><strong>{lead}</strong></p>"


def build_reader_html(article: dict, url: str) -> str:
    title = escape(article.get("title") or url)
    dek = escape(article.get("dek") or "")
    author = escape(article.get("author") or "")
    published = escape(article.get("published_at") or "")
    source = escape(article.get("site_name") or "")
    raw_html = article.get("article_html") or ""
    p_count = len(re.findall(r"<p\b", raw_html, flags=re.I))
    if p_count >= 2:
        body_html = raw_html
    else:
        paras = _paragraphs(article.get("article_text") or article.get("description") or "")
        if paras:
            chunks = [_lead_html(paras[0])]
            chunks.extend(f"<p>{escape(p)}</p>" for p in paras[1:])
            body_html = "\n".join(chunks)
        elif raw_html:
            body_html = raw_html
        else:
            body_html = "<p></p>"

    initial = escape((article.get("author") or source or "?").strip()[:1].upper() or "?")
    photo = article.get("author_image") or ""
    avatar = (
        f'<img class="avatar" src="{escape(photo)}" alt="">'
        if photo
        else f'<div class="avatar letter">{initial}</div>'
    )
    notice = ""
    if article.get("paywalled"):
        retried = (
            " Amber retried as a Google referrer visit and still only got a teaser."
            if article.get("referrer_retried")
            else ""
        )
        notice = (
            '<p class="notice">Incomplete — this visit only kept a paywalled teaser.'
            f"{retried} "
            "The rest of the article was not in the page a logged-out visitor received. "
            "To keep the full piece, save the page as HTML from a logged-in browser, then "
            "Import that file on Amber's homepage.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: #fff; color: #111;
      font-family: "Iowan Old Style", "Palatino Linotype", Palatino, "Times New Roman", serif;
    }}
    .wrap {{ max-width: 980px; margin: 0 auto; padding: 2.2rem 1.5rem 4rem; }}
    h1 {{
      font-size: clamp(2rem, 4.4vw, 3.15rem);
      line-height: 1.12; font-weight: 700; letter-spacing: -0.02em;
      margin: 0 0 0.85rem;
    }}
    .dek {{
      font-size: 1.28rem; line-height: 1.4; color: #222;
      margin: 0 0 2.2rem; font-weight: 400;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 220px 1fr;
      gap: 2.5rem;
      align-items: start;
    }}
    .by {{
      display: flex; align-items: center; gap: 0.65rem;
      font-size: 0.95rem; margin-bottom: 1.1rem;
    }}
    .avatar {{
      width: 42px; height: 42px; border-radius: 50%; object-fit: cover;
      background: #eee; flex: none;
    }}
    .avatar.letter {{
      display: grid; place-items: center; font-weight: 700; color: #444;
    }}
    .meta {{
      font-size: 0.92rem; color: #666; line-height: 1.55;
      padding-bottom: 1.1rem; border-bottom: 1px solid #e6e6e6;
    }}
    .meta .k {{ color: #888; }}
    .body {{ font-size: 1.18rem; line-height: 1.55; }}
    .body p {{ margin: 0 0 1.25rem; }}
    .body a {{ color: #111; }}
    .notice {{
      background: #fff4e5;
      border: 1px solid #e6c48a;
      color: #5c3d0e;
      font-family: "IBM Plex Sans", system-ui, sans-serif;
      font-size: 0.95rem;
      line-height: 1.45;
      padding: 0.85rem 1rem;
      margin: 0 0 1.5rem;
    }}
    @media (max-width: 720px) {{
      .grid {{ grid-template-columns: 1fr; gap: 1.2rem; }}
      h1 {{ font-size: 1.85rem; }}
    }}
  </style>
</head>
<body>
  <article class="wrap">
    <h1>{title}</h1>
    {f'<p class="dek">{dek}</p>' if dek else ''}
    <div class="grid">
      <aside>
        <div class="by">{avatar}<div>By {author or "Unknown"}</div></div>
        <div class="meta">
          {f'{published}<br>' if published else ''}
          {f'<span class="k">Source:</span> {source}' if source else ''}
        </div>
      </aside>
      <div class="body">
        {notice}
        {body_html}
      </div>
    </div>
  </article>
</body>
</html>
"""
