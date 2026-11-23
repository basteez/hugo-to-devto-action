from __future__ import annotations

import base64
import datetime as dt
import json
import zlib

import pytest

from crosspost.models import PostFile
from crosspost.transform import (
    TransformConfigError,
    TransformOptions,
    absolutize_images,
    mermaid_image_url,
    mermaid_to_images,
    post_url,
    prepare,
    resolve_url,
    urlize,
)

SITE = "https://example.com"
PAGE = "https://example.com/p/my-post/"


def _post(body: str = "Hello.\n", path: str = "content/post/2026-04-17-my-post/index.md", **fm) -> PostFile:
    front = {"title": "My Post", "draft": False}
    front.update(fm)
    return PostFile(path=path, frontmatter=front, body=body)


def _decode_pako(url: str) -> dict:
    data = url.split("/img/pako:", 1)[1].split("?", 1)[0]
    data += "=" * (-len(data) % 4)
    return json.loads(zlib.decompress(base64.urlsafe_b64decode(data)))


# --- options ---------------------------------------------------------------


def test_default_options_are_a_noop():
    post = _post(
        body="![a](img.png)\n\n```mermaid\ngraph LR\n  A-->B\n```\n",
        image="cover.png",
    )
    opts = TransformOptions()
    opts.validate()
    assert opts.is_noop
    prepared = prepare(post, opts)
    assert prepared.body_markdown == post.body
    assert prepared.canonical_url is None
    assert prepared.main_image is None


@pytest.mark.parametrize(
    "opts, message",
    [
        (TransformOptions(mermaid="svg"), "mermaid must be one of"),
        (TransformOptions(site_url="example.com", permalink_pattern="/:slug/"), "absolute"),
        (TransformOptions(site_url=SITE), "permalink-pattern is required"),
        (TransformOptions(mermaid="image", mermaid_service_url="mermaid.ink"), "mermaid-service-url"),
    ],
)
def test_invalid_options_are_rejected(opts, message):
    with pytest.raises(TransformConfigError, match=message):
        opts.validate()


# --- post URL --------------------------------------------------------------


def test_post_url_uses_front_matter_slug():
    post = _post(slug="bonooru-devlog-0")
    assert post_url(post, SITE + "/", "/p/:slug/") == "https://example.com/p/bonooru-devlog-0/"


def test_post_url_falls_back_to_urlized_title():
    post = _post(title="Bonooru Devlog #0: Teaching a Switch 2!")
    assert post_url(post, SITE, "/p/:slug/") == "https://example.com/p/bonooru-devlog-0-teaching-a-switch-2/"


def test_post_url_date_and_filename_tokens():
    post = _post(date=dt.date(2026, 4, 7))
    assert (
        post_url(post, SITE, "/:year/:month/:day/:filename/")
        == "https://example.com/2026/04/07/2026-04-17-my-post/"
    )
    single = _post(path="content/post/hello-world.md", date="2026-11-21T10:00:00+01:00")
    assert post_url(single, SITE, "/:year/:filename/") == "https://example.com/2026/hello-world/"


def test_post_url_front_matter_url_wins():
    post = _post(url="custom/path/", slug="ignored")
    assert post_url(post, SITE, "/p/:slug/") == "https://example.com/custom/path/"


def test_urlize():
    assert urlize("  Hello, World -- 2026  ") == "hello-world-2026"


# --- images ----------------------------------------------------------------


@pytest.mark.parametrize(
    "src, expected",
    [
        ("img.png", PAGE + "img.png"),
        ("./img.png", PAGE + "img.png"),
        ("sub/img.png", PAGE + "sub/img.png"),
        ("../shared/img.png", "https://example.com/p/shared/img.png"),
        ("/images/x.png", "https://example.com/images/x.png"),
        ("https://cdn.example.org/x.png", "https://cdn.example.org/x.png"),
        ("//cdn.example.org/x.png", "//cdn.example.org/x.png"),
        ("data:image/png;base64,AAAA", "data:image/png;base64,AAAA"),
    ],
)
def test_resolve_url(src, expected):
    assert resolve_url(src, PAGE, SITE) == expected


def test_absolutize_images_markdown_html_and_titles():
    body = (
        '![one](one.png)\n'
        '![two](two.jpg "A title")\n'
        '![three](<with space.png>)\n'
        '<img src="four.png" alt="four">\n'
        "[a link](page.html) stays\n"
        "![abs](https://x.org/a.png)\n"
    )
    out = absolutize_images(body, PAGE, SITE)
    assert f"![one]({PAGE}one.png)" in out
    assert f'![two]({PAGE}two.jpg "A title")' in out
    assert f"![three](<{PAGE}with space.png>)" in out
    assert f'<img src="{PAGE}four.png" alt="four">' in out
    assert "[a link](page.html) stays" in out
    assert "![abs](https://x.org/a.png)" in out


def test_absolutize_images_leaves_code_blocks_alone():
    body = "```markdown\n![x](x.png)\n```\n\n![y](y.png)\n"
    out = absolutize_images(body, PAGE, SITE)
    assert "```markdown\n![x](x.png)\n```" in out
    assert f"![y]({PAGE}y.png)" in out


# --- mermaid ---------------------------------------------------------------


def test_mermaid_image_url_round_trips():
    code = "graph LR\n  A-->B\n"
    url = mermaid_image_url(code, "https://mermaid.ink/")
    assert url.startswith("https://mermaid.ink/img/pako:")
    assert url.endswith("?type=png&bgColor=white")
    assert _decode_pako(url)["code"] == code


def test_mermaid_to_images_with_hugo_caption():
    body = (
        "Intro.\n\n"
        '```mermaid {caption="The whole system"}\n'
        "flowchart LR\n  A --> B\n"
        "```\n\n"
        "```python\nprint('kept')\n```\n"
        "```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n"
    )
    out, count = mermaid_to_images(body, "https://mermaid.ink")
    assert count == 2
    assert "```mermaid" not in out
    assert "```python\nprint('kept')\n```" in out
    assert "![The whole system](https://mermaid.ink/img/pako:" in out
    assert "\n*The whole system*\n" in out
    assert "![Diagram 2](https://mermaid.ink/img/pako:" in out
    first_url = out.split("![The whole system](", 1)[1].split(")", 1)[0]
    assert _decode_pako(first_url)["code"] == "flowchart LR\n  A --> B\n"


def test_mermaid_in_longer_fence_is_not_converted():
    body = "````markdown\n```mermaid\ngraph LR\n```\n````\n"
    out, count = mermaid_to_images(body, "https://mermaid.ink")
    assert count == 0
    assert out == body


def test_unclosed_fence_is_left_alone():
    body = "```mermaid\ngraph LR\n  A-->B\n"
    out, count = mermaid_to_images(body, "https://mermaid.ink")
    assert (out, count) == (body, 0)


# --- prepare ---------------------------------------------------------------


def test_prepare_with_everything_enabled():
    post = _post(
        body='![wiring](wiring.png)\n\n```mermaid {caption="Flow"}\ngraph LR\n  A-->B\n```\n',
        slug="my-post",
        image="cover.jpg",
    )
    opts = TransformOptions(site_url=SITE, permalink_pattern="/p/:slug/", mermaid="image")
    opts.validate()
    prepared = prepare(post, opts)
    assert prepared.canonical_url == PAGE
    assert prepared.main_image == PAGE + "cover.jpg"
    assert f"![wiring]({PAGE}wiring.png)" in prepared.body_markdown
    assert "![Flow](https://mermaid.ink/img/pako:" in prepared.body_markdown
    assert "```mermaid" not in prepared.body_markdown


def test_prepare_mermaid_only_sends_no_canonical():
    post = _post(body="```mermaid\ngraph LR\n```\n![a](a.png)\n", image="cover.jpg")
    prepared = prepare(post, TransformOptions(mermaid="image"))
    assert prepared.canonical_url is None
    assert prepared.main_image is None
    assert "![a](a.png)" in prepared.body_markdown
