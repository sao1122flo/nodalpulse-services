"""HTML and plain-text email template builders for NodalPulse daily briefs."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

_CHICAGO = ZoneInfo("America/Chicago")

# Section display labels
_SECTION_LABELS = {
    "top_of_mind": "Top of mind",
    "what_changed": "What changed",
    "docket_updates": "Tracked dockets",
}

# System-safe font stack — Outlook strips Google Fonts; this degrades gracefully
_FONT_STACK = (
    "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "Roboto, Helvetica, Arial, sans-serif"
)
_MONO_STACK = (
    "'JetBrains Mono', 'Fira Code', 'Cascadia Code', "
    "Consolas, 'Courier New', monospace"
)


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _base_styles() -> str:
    return f"""
    body {{
        font-family: {_FONT_STACK};
        font-size: 14px;
        line-height: 1.55;
        color: #44403C;
        background: #FFFFFF;
        margin: 0;
        padding: 0;
    }}
    .wrapper {{
        max-width: 600px;
        margin: 0 auto;
        padding: 24px 16px;
    }}
    .header {{
        border-bottom: 1px solid #E5E5E7;
        padding-bottom: 16px;
        margin-bottom: 8px;
        overflow: hidden;
    }}
    .logo {{
        font-size: 18px;
        font-weight: 700;
        letter-spacing: -0.02em;
        color: #18181B;
        text-decoration: none;
    }}
    .logo-pulse {{
        color: #6366F1;
    }}
    .header-meta {{
        float: right;
        font-size: 13px;
        color: #71717A;
        margin-top: 2px;
    }}
    .section-title {{
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: #71717A;
        margin: 24px 0 12px;
        border-top: 1px solid #E5E5E7;
        padding-top: 16px;
    }}
    .item {{
        padding-bottom: 20px;
        margin-bottom: 20px;
        border-bottom: 1px solid #F1F1F3;
    }}
    .item-header {{
        overflow: hidden;
        margin-bottom: 4px;
    }}
    .item-title {{
        font-size: 15px;
        font-weight: 600;
        color: #18181B;
        line-height: 1.3;
        margin: 0;
    }}
    .item-summary {{
        font-size: 14px;
        color: #44403C;
        margin: 6px 0 8px;
        line-height: 1.55;
    }}
    .citation {{
        font-family: {_MONO_STACK};
        font-size: 11px;
        color: #6366F1;
        text-decoration: none;
    }}
    .cta {{
        display: inline-block;
        font-size: 12px;
        font-weight: 500;
        color: #6366F1;
        text-decoration: none;
        float: right;
        margin-left: 12px;
    }}
    .footer {{
        border-top: 1px solid #E5E5E7;
        padding-top: 16px;
        margin-top: 24px;
        font-size: 12px;
        color: #71717A;
    }}
    .footer a {{
        color: #71717A;
        text-decoration: underline;
    }}
    .footer-stamp {{
        margin-top: 8px;
        display: block;
        font-family: {_MONO_STACK};
        font-size: 11px;
    }}
    """


def build_brief_html(
    *,
    brief_date: date,
    sections: dict[str, list[dict]],
    generated_at: datetime,
    composer_version: str,
    app_url: str,
    unsubscribe_url: str,
    eval_ok: bool = True,
    item_count: int,
) -> str:
    date_str = brief_date.strftime("%A, %B %-d, %Y")
    gen_ct = generated_at.astimezone(_CHICAGO).strftime("%H:%M CT")
    eval_label = "evals green" if eval_ok else "evals warning"

    items_html = ""
    for section_key in ("top_of_mind", "what_changed", "docket_updates"):
        items = sections.get(section_key, [])
        if not items:
            continue
        label = _esc(_SECTION_LABELS[section_key])
        items_html += f'<div class="section-title">{label}</div>\n'
        for item in items:
            filing_url = item.get("source_url") or f"{app_url}/filing/{item['filing_id']}"
            items_html += f"""<div class="item">
  <div class="item-header">
    <a href="{filing_url}" class="cta">Open &#x2192;</a>
    <div class="item-title">{_esc(item['title'])}</div>
  </div>
  <div class="item-summary">{_esc(item['summary'])}</div>
  <a href="{filing_url}" class="citation">{_esc(item['citation'])}</a>
</div>
"""

    item_word = "item" if item_count == 1 else "items"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>NodalPulse &middot; {_esc(date_str)}</title>
  <style>{_base_styles()}</style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <a href="{app_url}" class="logo">Nodal<span class="logo-pulse">Pulse</span></a>
      <div class="header-meta">{_esc(date_str)} &middot; {item_count} {item_word}</div>
    </div>
    {items_html}
    <div class="footer">
      <a href="{app_url}/dashboard">View in app</a>
      &nbsp;&middot;&nbsp;
      <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;&middot;&nbsp;
      <a href="https://nodalpulse.com/status">Status</a>
      <span class="footer-stamp">
        v{_esc(composer_version)} &middot; generated {_esc(gen_ct)} &middot; {_esc(eval_label)}
      </span>
    </div>
  </div>
</body>
</html>"""


def build_quiet_day_html(
    *,
    brief_date: date,
    corpus_count: int,
    app_url: str,
    unsubscribe_url: str,
) -> str:
    date_str = brief_date.strftime("%A, %B %-d, %Y")
    digest_url = f"https://nodalpulse.com/digest/{brief_date.isoformat()}"
    filing_word = "filing" if corpus_count == 1 else "filings"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>NodalPulse &middot; Quiet day &middot; {_esc(date_str)}</title>
  <style>
    body {{
        font-family: {_FONT_STACK};
        font-size: 14px;
        line-height: 1.55;
        color: #44403C;
        background: #FFFFFF;
        margin: 0;
        padding: 0;
    }}
    .wrapper {{ max-width: 600px; margin: 0 auto; padding: 24px 16px; }}
    .footer {{
        border-top: 1px solid #E5E5E7;
        padding-top: 16px;
        margin-top: 24px;
        font-size: 12px;
        color: #71717A;
    }}
    .footer a {{ color: #71717A; text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <p style="font-size:15px;font-weight:600;color:#18181B;margin:0 0 12px">
      Quiet day &mdash; {_esc(date_str)}
    </p>
    <p style="margin:0 0 16px">
      0 items match your filters.
      The full corpus had {corpus_count} {filing_word}.
      <a href="{digest_url}" style="color:#6366F1">View the public digest.</a>
    </p>
    <div class="footer">
      <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;&middot;&nbsp;
      <a href="https://nodalpulse.com/status">Status</a>
    </div>
  </div>
</body>
</html>"""


def build_maintenance_html(*, brief_date: date, unsubscribe_url: str) -> str:
    date_str = brief_date.strftime("%A, %B %-d, %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>NodalPulse &middot; Pipeline maintenance</title>
  <style>
    body {{
        font-family: {_FONT_STACK};
        font-size: 14px;
        line-height: 1.55;
        color: #44403C;
        background: #FFFFFF;
        margin: 0;
        padding: 0;
    }}
    .wrapper {{ max-width: 600px; margin: 0 auto; padding: 24px 16px; }}
    .footer {{
        border-top: 1px solid #E5E5E7;
        padding-top: 16px;
        margin-top: 24px;
        font-size: 12px;
        color: #71717A;
    }}
    .footer a {{ color: #71717A; text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="wrapper">
    <p style="font-size:15px;font-weight:600;color:#18181B;margin:0 0 12px">
      Pipeline maintenance &mdash; {_esc(date_str)}
    </p>
    <p style="margin:0 0 16px">
      Today&rsquo;s brief is paused while the extraction pipeline is under maintenance.
      Check
      <a href="https://nodalpulse.com/status" style="color:#6366F1">nodalpulse.com/status</a>
      for updates.
    </p>
    <div class="footer">
      <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;&middot;&nbsp;
      <a href="https://nodalpulse.com/status">Status</a>
    </div>
  </div>
</body>
</html>"""


def build_brief_text(
    *,
    brief_date: date,
    sections: dict[str, list[dict]],
    app_url: str,
    unsubscribe_url: str,
    composer_version: str,
) -> str:
    date_str = brief_date.strftime("%A, %B %d, %Y")
    lines = [f"NodalPulse — {date_str}", "=" * 52, ""]

    for section_key in ("top_of_mind", "what_changed", "docket_updates"):
        items = sections.get(section_key, [])
        if not items:
            continue
        label = _SECTION_LABELS[section_key].upper()
        lines += [f"── {label} ──", ""]
        for item in items:
            lines += [
                item["title"],
                item["summary"],
                item["citation"],
                f"  {app_url}/filing/{item['filing_id']}",
                "",
            ]

    lines += [
        "-" * 52,
        f"View in app:  {app_url}/dashboard",
        f"Unsubscribe:  {unsubscribe_url}",
        f"Status:       https://nodalpulse.com/status",
        f"v{composer_version}",
    ]
    return "\n".join(lines)
