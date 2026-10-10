import os
from html import escape

# Palette taken from the HackMatrix 26 banner: cream page, white card, red-orange accent, near-black ink.
PAGE = "#fbf1e4"
INK = "#1f1a17"
BODY = "#57534e"
MUTED = "#8a817a"
PANEL = "#faf6f0"
RULE = "#f0e2d0"
ACCENT = "#e8452c"
ACCENT_SOFT = "#fde0d6"
# Tints of the banner's red-orange and cream for the numbered steps and footer dots.
TINTS = ("#fde0d6", "#fbe9d0", "#f9cfc2")
SANS = "'Helvetica Neue',Helvetica,Arial,sans-serif"
SERIF = "Georgia,'Times New Roman',serif"

BANNER_ALT = "HackMatrix 26 · Hack Day · 14 Nov 2026 · Adamas University"


def banner_url() -> str:
    """The banner is hosted by the participant site; EMAIL_BANNER_URL overrides it (staging, previews)."""
    return os.getenv("EMAIL_BANNER_URL") or f"{os.getenv('PARTICIPANT_APP_URL', 'http://localhost:3000').rstrip('/')}/email/hackmatrix-26-banner.gif"


def _text(value: str) -> str:
    """Escapes user or staff text and keeps its line breaks."""
    return escape(value).replace("\r\n", "\n").replace("\n", "<br>")


def _banner(url: str | None = None, alt: str = BANNER_ALT) -> str:
    site = escape(os.getenv("PARTICIPANT_APP_URL", "https://hackmatrix.codes"), quote=True)
    return (
        f'<a href="{site}" style="text-decoration:none;"><img src="{escape(url or banner_url(), quote=True)}" width="564" alt="{escape(alt, quote=True)}" '
        f'style="display:block;width:100%;max-width:564px;height:auto;border:0;border-radius:20px;background:{PAGE};font-family:{SANS};font-size:14px;line-height:1.4;color:{ACCENT};"></a>'
    )


def _details(rows: list[tuple[str, str]], original_amount: str | None) -> str:
    if not rows:
        return ""
    items = []
    for index, (label, value) in enumerate(rows):
        border = "" if index == len(rows) - 1 else f"border-bottom:1px dashed {RULE};"
        struck = f'<s style="color:{MUTED};font-weight:400;">{escape(original_amount)}</s>&nbsp; ' if label in ("Amount due", "Amount paid") and original_amount else ""
        items.append(
            f"""<tr>
              <td style="padding:14px 0;{border}font-family:{SANS};font-size:12px;letter-spacing:0.08em;text-transform:uppercase;color:{MUTED};">{escape(label)}</td>
              <td style="padding:14px 0;{border}font-family:{SANS};font-size:15px;font-weight:700;color:{INK};text-align:right;">{struck}{escape(value)}</td>
            </tr>"""
        )
    return f"""<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:28px;background:{PANEL};border-radius:20px;">
      <tr><td style="padding:6px 22px;"><table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(items)}</table></td></tr>
    </table>"""


def _steps(steps: list[str]) -> str:
    if not steps:
        return ""
    items = "".join(
        f"""<tr>
          <td style="width:44px;padding:0 0 14px;vertical-align:top;">
            <div style="width:30px;height:30px;border-radius:15px;background:{TINTS[(index - 1) % len(TINTS)]};text-align:center;font-family:{SANS};font-size:13px;font-weight:700;line-height:30px;color:{INK};">{index}</div>
          </td>
          <td style="padding:5px 0 14px;font-family:{SANS};font-size:15px;line-height:1.55;color:{BODY};">{escape(step)}</td>
        </tr>"""
        for index, step in enumerate(steps, start=1)
    )
    return f"""<p style="margin:34px 0 14px;font-family:{SANS};font-size:12px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{MUTED};">What happens next</p>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{items}</table>"""


def action_email(
    *,
    title: str,
    intro: str,
    rows: list[tuple[str, str]],
    steps: list[str],
    action: str,
    url: str,
    note: str,
    original_amount: str | None = None,
    eyebrow: str = "HackMatrix 26",
) -> str:
    content = f"""<span style="display:inline-block;padding:6px 14px;border-radius:999px;background:{ACCENT_SOFT};font-family:{SANS};font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:{ACCENT};">{escape(eyebrow)}</span>
                      <h1 class="headline" style="margin:18px 0 0;font-family:{SERIF};font-size:34px;font-weight:400;line-height:1.18;letter-spacing:-0.5px;color:{INK};">{escape(title)}</h1>
                      <p style="margin:16px 0 0;font-family:{SANS};font-size:16px;line-height:1.7;color:{BODY};">{_text(intro)}</p>
                      {_details(rows, original_amount)}
                      {_steps(steps)}
                      {button(action, url)}
                      <p style="margin:26px 0 0;font-family:{SANS};font-size:13px;line-height:1.6;color:{MUTED};">{_text(note)}</p>
                      {link_fallback(url)}"""
    return email_shell(title=title, preheader=intro, banner=_banner(), content=content)


def button(label: str, url: str) -> str:
    return f"""<table role="presentation" cellspacing="0" cellpadding="0" style="margin-top:32px;"><tr>
                        <td style="border-radius:999px;background:{INK};">
                          <a href="{escape(url, quote=True)}" style="display:inline-block;padding:16px 30px;border-radius:999px;font-family:{SANS};font-size:15px;font-weight:700;letter-spacing:0.02em;color:#fffaf3;text-decoration:none;">{escape(label)}&nbsp;&nbsp;&rarr;</a>
                        </td>
                      </tr></table>"""


def link_fallback(url: str) -> str:
    return f"""<p style="margin:14px 0 0;padding:12px 16px;border-radius:14px;background:{PANEL};font-family:{SANS};font-size:12px;line-height:1.5;color:{MUTED};">Button not working? Paste this link into your browser:<br><span style="color:{BODY};word-break:break-all;">{escape(url)}</span></p>"""


def email_shell(*, title: str, preheader: str, banner: str, content: str) -> str:
    """The branded page every HackMatrix email shares; `banner` may be empty for a text-only email."""
    preheader = escape(" ".join(preheader.split())[:140])
    banner_row = f"""<tr><td class="banner" style="padding:18px 18px 0;">{banner}</td></tr>""" if banner else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
    <title>{escape(title)}</title>
    <style>
      @media (max-width:620px) {{
        .card {{ padding:30px 22px !important; }}
        .headline {{ font-size:28px !important; }}
        .banner {{ padding:12px 12px 0 !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:{PAGE};">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{PAGE};">{preheader}</div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:{PAGE};padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:600px;">
            <tr>
              <td style="padding:0 6px 18px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
                  <td style="font-family:{SERIF};font-size:20px;font-style:italic;color:{INK};">HackMatrix<span style="color:{ACCENT};font-style:normal;">&nbsp;&#10022;&nbsp;</span>26</td>
                  <td align="right"><span style="display:inline-block;padding:6px 12px;border-radius:999px;background:#ffffff;font-family:{SANS};font-size:11px;letter-spacing:0.06em;color:{MUTED};">ACM &middot; Adamas University</span></td>
                </tr></table>
              </td>
            </tr>
            <tr>
              <td style="background:#ffffff;border-radius:28px;box-shadow:0 18px 40px rgba(31,26,23,0.08);">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  {banner_row}
                  <tr>
                    <td class="card" style="padding:34px 40px 40px;">
                      {content}
                    </td>
                  </tr>
                </table>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:26px 12px 0;font-family:{SANS};font-size:12px;line-height:1.7;color:{MUTED};">
                <span style="color:{ACCENT};">&#10022;</span>&nbsp;<span style="color:{TINTS[2]};">&#9679;</span>&nbsp;<span style="color:{ACCENT};">&#10022;</span><br>
                Made with care by ACM Student Chapter, Adamas University &middot; Kolkata<br>
                If you did not expect this message, you can safely ignore it.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
