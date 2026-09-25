from html import escape

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
) -> str:
    detail_rows = "".join(
        f"""<tr>
          <td style="padding:12px 0;border-bottom:1px solid #f3e8d8;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#78716c;">{escape(label)}</td>
          <td style="padding:12px 0;border-bottom:1px solid #f3e8d8;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1c1917;font-weight:700;text-align:right;">{f'<s style="color:#78716c">{escape(original_amount)}</s> ' if label in ('Amount due', 'Amount paid') and original_amount else ''}{escape(value)}</td>
        </tr>"""
        for label, value in rows
    )
    step_rows = "".join(
        f"""<tr>
          <td style="width:36px;vertical-align:top;font-family:Arial,Helvetica,sans-serif;font-size:14px;font-weight:700;color:#9a3412;">{index}.</td>
          <td style="padding:0 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#44403c;">{escape(step)}</td>
        </tr>"""
        for index, step in enumerate(steps, start=1)
    )
    return f"""<!doctype html>
<html>
  <body style="margin:0;background:#ffffff;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ffffff;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;">
            <tr>
              <td style="padding:8px 8px 18px;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#78716c;">HackMatrix 26</td>
            </tr>
            <tr>
              <td style="background:#ffffff;padding:24px 8px;">
                <p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:13px;color:#9a3412;">HackMatrix 26</p>
                <h1 style="margin:10px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:32px;line-height:1.15;color:#1c1917;">{escape(title)}</h1>
                <p style="margin:16px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:16px;line-height:1.6;color:#44403c;">{escape(intro)}</p>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:28px;">{detail_rows}</table>
                <h2 style="margin:32px 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:18px;color:#1c1917;">What happens next</h2>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{step_rows}</table>
                <p style="margin:28px 0 0;">
                  <a href="{escape(url, quote=True)}" style="display:inline-block;background:#9a3412;color:#fff7ed;font-family:Arial,Helvetica,sans-serif;font-size:16px;font-weight:700;text-decoration:none;border-radius:8px;padding:14px 22px;">{escape(action)}</a>
                </p>
                <p style="margin:22px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:#78716c;">{escape(note)}</p>
                <p style="margin:8px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:#78716c;word-break:break-all;">{escape(url)}</p>
              </td>
            </tr>
            <tr>
              <td style="padding:18px 8px 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.6;color:#78716c;">
                HackMatrix · ACM Student Chapter, Adamas University · Kolkata<br>
                If you did not request this message, you can ignore it.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
