"""Staff-written Markdown emails in the HackMatrix layout.

Email clients ignore <style> blocks unevenly, so every element the Markdown produces carries
inline styles. Raw HTML in the source is escaped: what staff see in the preview is exactly
what is sent, and a stray tag can never break the layout or inject markup.
"""

from html import escape

import mistune

from src.communications.templates import ACCENT, ACCENT_SOFT, BODY, INK, MUTED, PANEL, RULE, SANS, SERIF, _banner, button, email_shell, link_fallback

# Placeholders replaced per recipient; values are escaped before they enter the HTML.
PLACEHOLDERS = ("name", "first_name", "team_name", "email")
SAMPLE = {"name": "Alex Sharma", "first_name": "Alex", "team_name": "Team Matrix", "email": "alex@example.com"}

TEXT = f"font-family:{SANS};font-size:16px;line-height:1.7;color:{BODY};"
HEADINGS = {
    1: f"margin:28px 0 0;font-family:{SERIF};font-size:30px;font-weight:400;line-height:1.2;color:{INK};",
    2: f"margin:26px 0 0;font-family:{SERIF};font-size:24px;font-weight:400;line-height:1.25;color:{INK};",
    3: f"margin:22px 0 0;font-family:{SANS};font-size:18px;font-weight:700;line-height:1.35;color:{INK};",
}


class EmailRenderer(mistune.HTMLRenderer):
    def paragraph(self, text: str) -> str:
        return f'<p style="margin:16px 0 0;{TEXT}">{text}</p>\n'

    def block_html(self, html: str) -> str:
        return self.paragraph(escape(html))

    def inline_html(self, html: str) -> str:
        return escape(html)

    def heading(self, text: str, level: int, **attrs) -> str:
        return f'<h{level} style="{HEADINGS.get(level, HEADINGS[3])}">{text}</h{level}>\n'

    def link(self, text: str, url: str, title: str | None = None) -> str:
        title_attr = f' title="{escape(title, quote=True)}"' if title else ""
        return f'<a href="{self.safe_url(url)}"{title_attr} style="color:{ACCENT};text-decoration:underline;">{text}</a>'

    def image(self, text: str, url: str, title: str | None = None) -> str:
        title_attr = f' title="{escape(title, quote=True)}"' if title else ""
        return (
            f'<img src="{self.safe_url(url)}" alt="{escape(text, quote=True)}"{title_attr} width="520" '
            f'style="display:block;width:100%;max-width:520px;height:auto;margin:8px 0;border:0;border-radius:16px;">'
        )

    def strong(self, text: str) -> str:
        return f'<strong style="color:{INK};">{text}</strong>'

    def codespan(self, text: str) -> str:
        return f'<code style="padding:2px 6px;border-radius:6px;background:{PANEL};font-family:Menlo,Consolas,monospace;font-size:14px;color:{INK};">{escape(text)}</code>'

    def block_code(self, code: str, info: str | None = None) -> str:
        return (
            f'<pre style="margin:16px 0 0;padding:14px 16px;border-radius:14px;background:{PANEL};overflow-x:auto;'
            f'font-family:Menlo,Consolas,monospace;font-size:13px;line-height:1.55;color:{INK};white-space:pre-wrap;">{escape(code)}</pre>\n'
        )

    def block_quote(self, text: str) -> str:
        return f'<blockquote style="margin:16px 0 0;padding:0 18px 14px;border-left:4px solid {ACCENT};background:{ACCENT_SOFT};border-radius:0 14px 14px 0;">{text}</blockquote>\n'

    def thematic_break(self) -> str:
        return f'<hr style="margin:28px 0 0;border:0;border-top:1px dashed {RULE};">\n'

    def list(self, text: str, ordered: bool, **attrs) -> str:
        tag = "ol" if ordered else "ul"
        start = f' start="{attrs["start"]}"' if ordered and attrs.get("start") not in (None, 1) else ""
        return f'<{tag}{start} style="margin:16px 0 0;padding-left:24px;{TEXT}">{text}</{tag}>\n'

    def list_item(self, text: str) -> str:
        return f'<li style="margin:6px 0 0;">{text}</li>\n'

    def strikethrough(self, text: str) -> str:
        return f'<s style="color:{MUTED};">{text}</s>'

    def table(self, text: str) -> str:
        return f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:16px;border-collapse:collapse;">{text}</table>\n'

    def table_head(self, text: str) -> str:
        return f"<thead><tr>{text}</tr></thead>\n"

    def table_body(self, text: str) -> str:
        return f"<tbody>{text}</tbody>\n"

    def table_row(self, text: str) -> str:
        return f"<tr>{text}</tr>\n"

    def table_cell(self, text: str, align: str | None = None, head: bool = False) -> str:
        tag = "th" if head else "td"
        weight = f"font-weight:700;color:{INK};background:{PANEL};" if head else f"color:{BODY};"
        return f'<{tag} style="padding:10px 12px;border-bottom:1px dashed {RULE};text-align:{align or "left"};font-family:{SANS};font-size:14px;{weight}">{text}</{tag}>'


_markdown = mistune.create_markdown(renderer=EmailRenderer(escape=True), plugins=["strikethrough", "table", "url"])


def render_markdown(source: str) -> str:
    return str(_markdown(source))


def personalise(html: str, values: dict[str, str]) -> str:
    for key in PLACEHOLDERS:
        html = html.replace("{{" + key + "}}", escape(values.get(key, ""))).replace("{{ " + key + " }}", escape(values.get(key, "")))
    return html


def campaign_email(
    *,
    subject: str,
    body_markdown: str,
    show_banner: bool,
    banner_url: str,
    cta_label: str,
    cta_url: str,
    values: dict[str, str],
) -> str:
    content = f"""<h1 class="headline" style="margin:0;font-family:{SERIF};font-size:34px;font-weight:400;line-height:1.18;letter-spacing:-0.5px;color:{INK};">{escape(subject)}</h1>
                      {render_markdown(body_markdown)}"""
    if cta_label and cta_url:
        content += button(cta_label, cta_url) + link_fallback(cta_url)
    banner = _banner(banner_url or None, alt=subject) if show_banner else ""
    preheader = " ".join(body_markdown.replace("#", " ").replace("*", " ").split())
    return personalise(email_shell(title=subject, preheader=preheader, banner=banner, content=content), values)
