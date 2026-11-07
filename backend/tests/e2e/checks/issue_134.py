"""Check the UI bundle delivered by the local application over HTTP."""

import re


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
    return {"scripts": len(scripts), "stylesheets": len(styles)}
