"""Check the UI bundle delivered by the local application over HTTP."""

import re
from urllib.parse import urljoin


def check_chat_ui_bundle(ctx):
    response = ctx.rest.get("/chat", headers={"accept": "text/html"})
    ctx.require(response.status_code == 200, "chat page was not served")
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
    styles = re.findall(r'<link[^>]+href="([^"]+\.css)"', response.text)
    ctx.require(scripts and styles, "chat page is missing its built assets")
    script = ctx.rest.get(scripts[0])
    css = ctx.rest.get(styles[0])
    ctx.require(
        script.status_code == 200 and css.status_code == 200, "UI assets unavailable"
    )
    ctx.require(
        "今天想查点什么数据？" in script.text, "chat greeting missing from UI bundle"
    )
    ctx.require(
        "#faf9f5" in css.text.lower(), "warm page background missing from UI bundle"
    )
    ctx.require(
        "#d97757" in css.text.lower(), "terracotta accent missing from UI bundle"
    )
    compact_css = re.sub(r"\s+", "", css.text)
    ctx.require(
        '--font-serif:"NewsreaderVariable","NotoSerifSC","SourceHanSerifSC"'
        in compact_css,
        "Newsreader serif stack missing from UI bundle",
    )
    ctx.require(
        "--font-sans:system-ui,-apple-system," in compact_css,
        "system sans stack missing from UI bundle",
    )
    ctx.require(
        "--font-mono:" in compact_css, "mono font variable missing from UI bundle"
    )
    ctx.require(
        ".markdown{min-width:0;color:var(--ink);font-family:var(--font-serif);"
        "font-size:16px;line-height:1.7}" in compact_css,
        "assistant answer typography missing from UI bundle",
    )

    font_urls = re.findall(r"url\(([^)]*newsreader[^)]*)\)", css.text, re.IGNORECASE)
    ctx.require(len(font_urls) == 2, "Newsreader normal/italic assets missing")
    for font_url in font_urls:
        asset = ctx.rest.get(urljoin(str(css.url), font_url.strip("\"'")))
        ctx.require(
            asset.status_code == 200 and asset.content,
            f"font asset unavailable: {font_url}",
        )

    bundle_text = script.text + css.text
    ctx.require(
        "fonts.googleapis" not in bundle_text and "fonts.gstatic" not in bundle_text,
        "UI bundle references an external font CDN",
    )
    return {
        "scripts": len(scripts),
        "stylesheets": len(styles),
        "fonts": len(font_urls),
    }
