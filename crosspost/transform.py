"""Optional body rewrites applied before a post is sent to dev.to.

Everything here is opt-in: with the default settings the body and the
payload are left exactly as they were before these options existed.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Optional
from urllib.parse import quote, urljoin

from .models import PostFile

MERMAID_MODES = ("keep", "image")
DEFAULT_MERMAID_SERVICE = "https://mermaid.ink"


class TransformConfigError(ValueError):
    """Raised when the transform options are inconsistent."""


@dataclass(frozen=True)
class TransformOptions:
    site_url: str = ""
    permalink_pattern: str = ""
    mermaid: str = "keep"
    mermaid_service_url: str = DEFAULT_MERMAID_SERVICE

    def validate(self) -> None:
        if self.mermaid not in MERMAID_MODES:
            raise TransformConfigError(
                f"mermaid must be one of {', '.join(MERMAID_MODES)}, got {self.mermaid!r}"
            )
        if self.site_url and not re.match(r"^https?://[^/\s]+", self.site_url):
            raise TransformConfigError(
                f"site-url must be an absolute http(s) URL, got {self.site_url!r}"
            )
        if self.site_url and not self.permalink_pattern:
            raise TransformConfigError(
                "permalink-pattern is required when site-url is set "
                "(for example /posts/:slug/ or /p/:slug/)"
            )
        if self.mermaid == "image" and not re.match(
            r"^https?://[^/\s]+", self.mermaid_service_url
        ):
            raise TransformConfigError(
                "mermaid-service-url must be an absolute http(s) URL, "
                f"got {self.mermaid_service_url!r}"
            )

    @property
    def is_noop(self) -> bool:
        return not self.site_url and self.mermaid == "keep"


@dataclass(frozen=True)
class PreparedArticle:
    body_markdown: str
    canonical_url: Optional[str] = None
    main_image: Optional[str] = None


def urlize(text: str) -> str:
    """Lower-case, keep letters/digits, collapse everything else to '-'."""
    slug = re.sub(r"[^\w]+", "-", text.strip().lower(), flags=re.UNICODE)
    return slug.strip("-_")


def _post_filename(path: str) -> str:
    p = PurePosixPath(path)
    if p.name in ("index.md", "_index.md"):
        return p.parent.name
    return p.stem


def _post_date(frontmatter: dict[str, Any]) -> Optional[_dt.date]:
    value = frontmatter.get("date")
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        try:
            return _dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def post_url(post: PostFile, site_url: str, permalink_pattern: str) -> str:
    """Public URL of a post on the Hugo site.

    A front matter `url` wins, as in Hugo. Otherwise the pattern is expanded
    with :slug (front matter `slug`, else the urlized title), :title,
    :filename, :year, :month and :day.
    """
    base = site_url.rstrip("/")
    fm = post.frontmatter
    explicit = fm.get("url")
    if isinstance(explicit, str) and explicit.strip():
        path = explicit.strip()
    else:
        title = post.title or ""
        slug_value = fm.get("slug")
        slug = (
            str(slug_value).strip()
            if slug_value is not None and str(slug_value).strip()
            else urlize(title)
        )
        date = _post_date(fm)
        tokens = {
            ":slug": slug,
            ":title": urlize(title),
            ":filename": _post_filename(post.path),
            ":year": f"{date.year:04d}" if date else "",
            ":month": f"{date.month:02d}" if date else "",
            ":day": f"{date.day:02d}" if date else "",
        }
        path = permalink_pattern
        # Longest tokens first so ":filename" is not eaten by a shorter one.
        for token in sorted(tokens, key=len, reverse=True):
            path = path.replace(token, quote(tokens[token], safe="-_.~"))
    if not path.startswith("/"):
        path = "/" + path
    return base + path


_ABSOLUTE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//|#)")
_MD_IMAGE = re.compile(r"(!\[[^\]]*\]\()(<[^>]+>|[^)\s]+)((?:\s+\"[^\"]*\")?\))")
_HTML_IMG = re.compile(r"(<img\b[^>]*?\bsrc=)([\"'])([^\"']+)(\2)", re.IGNORECASE)


def resolve_url(src: str, page_url: str, site_url: str) -> str:
    """Make a relative link absolute: '/x' against the site, 'x' against the page."""
    if _ABSOLUTE.match(src):
        return src
    if src.startswith("/"):
        return site_url.rstrip("/") + src
    page = page_url if page_url.endswith("/") else page_url + "/"
    return urljoin(page, src)


def absolutize_images(body: str, page_url: str, site_url: str) -> str:
    """Rewrite relative image sources (Markdown and <img>) to absolute URLs.

    Fenced code blocks are left untouched.
    """

    def md(m: re.Match[str]) -> str:
        src = m.group(2)
        wrapped = src.startswith("<") and src.endswith(">")
        inner = src[1:-1] if wrapped else src
        new = resolve_url(inner, page_url, site_url)
        return m.group(1) + (f"<{new}>" if wrapped else new) + m.group(3)

    def html(m: re.Match[str]) -> str:
        return m.group(1) + m.group(2) + resolve_url(m.group(3), page_url, site_url) + m.group(4)

    def rewrite(text: str) -> str:
        return _HTML_IMG.sub(html, _MD_IMAGE.sub(md, text))

    return _map_outside_fences(body, rewrite)


_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n", re.M)


def _split_fences(body: str) -> list[tuple[str, Optional[dict[str, str]]]]:
    """Split a body into ('text', None) and fenced blocks ('raw', {'info', 'code'})."""
    parts: list[tuple[str, Optional[dict[str, str]]]] = []
    pos = 0
    while True:
        m = _FENCE.search(body, pos)
        if not m:
            parts.append((body[pos:], None))
            return parts
        fence = m.group("fence")
        closing = re.compile(
            rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", re.M
        )
        c = closing.search(body, m.end())
        if not c:
            parts.append((body[pos:], None))
            return parts
        parts.append((body[pos : m.start()], None))
        end = c.end()
        parts.append(
            (
                body[m.start() : end],
                {"info": m.group("info").strip(), "code": body[m.end() : c.start()]},
            )
        )
        pos = end


def _map_outside_fences(body: str, fn) -> str:
    return "".join(
        fn(text) if block is None else text for text, block in _split_fences(body)
    )


_CAPTION = re.compile(r"""caption\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def mermaid_image_url(code: str, service_url: str) -> str:
    """Image URL for a Mermaid diagram, in the mermaid.ink 'pako' format."""
    payload = json.dumps(
        {"code": code, "mermaid": {"theme": "default"}}, separators=(",", ":")
    ).encode("utf-8")
    compressor = zlib.compressobj(9, zlib.DEFLATED, 15)
    packed = compressor.compress(payload) + compressor.flush()
    encoded = base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")
    return f"{service_url.rstrip('/')}/img/pako:{encoded}?type=png&bgColor=white"


def mermaid_to_images(body: str, service_url: str) -> tuple[str, int]:
    """Replace ```mermaid fences with images rendered by the service.

    A Hugo-style caption ({caption="..."}) becomes the alt text and an
    italic line under the image. Returns the new body and the count.
    """
    out: list[str] = []
    count = 0
    for text, block in _split_fences(body):
        if block is None:
            out.append(text)
            continue
        info = block["info"]
        words = info.split("{", 1)[0].split()
        lang = words[0].lower() if words else ""
        if lang != "mermaid":
            out.append(text)
            continue
        count += 1
        cap = _CAPTION.search(info)
        caption = (cap.group(1) or cap.group(2)) if cap else ""
        alt = (caption or f"Diagram {count}").replace("]", "\\]")
        image = f"![{alt}]({mermaid_image_url(block['code'], service_url)})\n"
        if caption:
            image += f"*{caption}*\n"
        out.append(image)
    return "".join(out), count


def prepare(post: PostFile, options: TransformOptions) -> PreparedArticle:
    """Apply the enabled rewrites to a post. A no-op with the defaults."""
    body = post.body
    canonical: Optional[str] = None
    main_image: Optional[str] = None

    if options.mermaid == "image":
        body, _ = mermaid_to_images(body, options.mermaid_service_url)

    if options.site_url:
        canonical = post_url(post, options.site_url, options.permalink_pattern)
        body = absolutize_images(body, canonical, options.site_url)
        image = post.frontmatter.get("image")
        if isinstance(image, str) and image.strip():
            main_image = resolve_url(image.strip(), canonical, options.site_url)

    return PreparedArticle(body_markdown=body, canonical_url=canonical, main_image=main_image)
