"""HTML and plain-text email template builders for NodalPulse daily briefs."""

from datetime import date, datetime, timedelta
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


def _deadline_badge(date_str: str | None, brief_date: date, label: str) -> str:
    """Render a coloured inline badge for an upcoming deadline/effective date.

    Color tiers: ≤1d red, ≤3d orange, ≤7d amber, ≤30d gray. Returns '' if
    date_str is None, unparseable, past, or more than 30 days away.
    """
    if not date_str:
        return ""
    try:
        d = date.fromisoformat(str(date_str)[:10])
        days = (d - brief_date).days
        if days < 0 or days > 30:
            return ""
        days_label = "today" if days == 0 else f"{days}d"
        if days <= 1:
            bg, fg = "#FEE2E2", "#991B1B"
        elif days <= 3:
            bg, fg = "#FED7AA", "#9A3412"
        elif days <= 7:
            bg, fg = "#FEF3C7", "#92400E"
        else:
            bg, fg = "#F3F4F6", "#4B5563"
        date_label = d.strftime("%b %-d")
        return (
            f'<span style="display:inline-block;font-size:11px;font-weight:600;'
            f'padding:2px 8px;border-radius:3px;background:{bg};color:{fg};'
            f'margin-bottom:4px;margin-right:4px">'
            f'{_esc(label)}: {_esc(date_label)} &middot; {_esc(days_label)}'
            f'</span>'
        )
    except (ValueError, AttributeError):
        return ""


def _protest_notice_badge(url: str | None) -> str:
    """Render a static 'Protest deadline — see FERC Notice' link badge.

    Never shows a computed date — scope B hard rule. Renders only when a
    verify_url is present so the user can navigate to FERC eLibrary directly.
    """
    if not url:
        return ""
    return (
        f'<a href="{url}" style="display:inline-block;font-size:11px;font-weight:600;'
        f'padding:2px 8px;border-radius:3px;background:#EFF6FF;color:#1D4ED8;'
        f'margin-bottom:4px;margin-right:4px;text-decoration:none">'
        f'Protest deadline &#x2192; FERC Notice'
        f'</a>'
    )


def _render_item(item: dict, app_url: str, brief_date: date) -> str:
    filing_url = item.get("source_url") or f"{app_url}/filing/{item['filing_id']}"
    dl_badge = _deadline_badge(item.get("nearest_deadline_date"), brief_date, "Deadline")
    eff_badge = _deadline_badge(item.get("nearest_effective_date"), brief_date, "Effective")
    protest_badge = _protest_notice_badge(item.get("protest_notice_url"))
    badges_html = (
        f'<div style="margin:4px 0 2px">{dl_badge}{eff_badge}{protest_badge}</div>\n'
        if (dl_badge or eff_badge or protest_badge) else ""
    )
    return (
        f'<div class="item">\n'
        f'  <div class="item-header">\n'
        f'    <a href="{filing_url}" class="cta">Open &#x2192;</a>\n'
        f'    <div class="item-title">{_esc(item["title"])}</div>\n'
        f'  </div>\n'
        f'  {badges_html}'
        f'  <div class="item-summary">{_esc(item["summary"])}</div>\n'
        f'  <a href="{filing_url}" class="citation">{_esc(item["citation"])}</a>\n'
        f'</div>\n'
    )


def _render_discovery_hit(hit: dict, app_url: str) -> str:
    dockets = ", ".join(hit.get("docket_numbers") or []) or "—"
    filers_raw = (hit.get("filer_names") or [])[:2]
    filers = ", ".join(filers_raw) if filers_raw else ""
    filed = str(hit.get("filed_at") or "")[:10]
    first_docket = (hit.get("docket_numbers") or [""])[0]
    track_url = (
        f"{app_url}/dockets?q={first_docket}" if first_docket else f"{app_url}/dockets"
    )
    meta = _esc(dockets)
    if filed:
        meta += f" &middot; {_esc(filed)}"
    if filers:
        meta += f" &middot; {_esc(filers)}"
    return (
        f'<div class="item">\n'
        f'  <div class="item-header">\n'
        f'    <a href="{_esc(track_url)}" class="cta">Track this &#x2192;</a>\n'
        f'    <div class="item-title">{_esc(str(hit.get("description") or "Filing"))}</div>\n'
        f'  </div>\n'
        f'  <div class="item-summary" style="font-size:12px;color:#6B7280">{meta}</div>\n'
        f'</div>\n'
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


def _render_calendar_events(events: list[dict]) -> str:
    """Render PJM upcoming deadline rows as an HTML section."""
    if not events:
        return ""
    rows = ""
    for ev in events:
        d = ev.get("event_date", "")
        title = _esc(str(ev.get("title", ""))[:120])
        source = _esc(str(ev.get("source", "")))
        estimated = ev.get("estimated", True)
        est_badge = (
            " <span style=\"font-size:10px;color:#9CA3AF;font-style:italic\">(est.)</span>"
            if estimated else ""
        )
        url = ev.get("source_url")
        if url:
            linked = (
                f"<a href=\"{_esc(url)}\" style=\"color:#374151;text-decoration:none\">"
                f"{title}</a>"
            )
        else:
            linked = title
        rows += (
            f"<tr>"
            f"<td style=\"padding:4px 12px 4px 0;white-space:nowrap;color:#6B7280;"
            f"font-size:12px;font-weight:500\">{_esc(d)}</td>"
            f"<td style=\"padding:4px 0;font-size:13px;color:#374151\">"
            f"{linked}{est_badge}</td>"
            f"<td style=\"padding:4px 0 4px 12px;white-space:nowrap;font-size:11px;"
            f"color:#9CA3AF\">{source}</td>"
            f"</tr>\n"
        )
    return (
        "<div class=\"section-title\">PJM UPCOMING &mdash; next 30 days</div>\n"
        "<table style=\"width:100%;border-collapse:collapse;margin-bottom:24px\">\n"
        f"<tbody>{rows}</tbody></table>\n"
    )


def build_brief_html(
    *,
    brief_date: date,
    sections: dict[str, list[dict]],
    docket_sections: list[dict] = (),
    generated_at: datetime,
    composer_version: str,
    app_url: str,
    unsubscribe_url: str,
    eval_ok: bool = True,
    item_count: int,
    filters_active: bool = True,
    calendar_events: list[dict] = (),
    discovery_hits: list[dict] = (),
) -> str:
    """Build the HTML email for a daily brief.

    filters_active=False: prepend an "Add filters" banner for users who have
    not yet configured markets, dockets, or saved searches. This is the global-
    fallback path for new or skipped-onboarding users.

    docket_sections: list of {"external_id", "pool_total", "items"} for the
    per-docket grouping (personalized path). Empty for global/flat path.
    """
    date_str = brief_date.strftime("%A, %B %-d, %Y")
    gen_ct = generated_at.astimezone(_CHICAGO).strftime("%H:%M CT")
    banner_html = ""
    if not filters_active:
        banner_html = (
            "<div style=\"background:#F5F3FF;border:1px solid #DDD6FE;border-radius:6px;"
            "padding:12px 16px;margin:16px 0;font-size:13px;color:#4C1D95;line-height:1.5\">"
            "<strong>Personalize this brief</strong> &mdash; "
            "Add markets, tracked dockets, or keyword searches to focus on filings that "
            "matter to you. Today&#39;s brief shows all sources."
            f"&nbsp;<a href=\"{app_url}/settings\" "
            "style=\"color:#6366F1;text-decoration:underline\">Add filters &rarr;</a>"
            "</div>\n"
        )

    items_html = ""

    # TOP_OF_MIND section
    tom_items = sections.get("top_of_mind", [])
    if tom_items:
        _lbl_tom = _esc(_SECTION_LABELS["top_of_mind"])
        items_html += f"<div class=\"section-title\">{_lbl_tom}</div>\n"
        for item in tom_items:
            items_html += _render_item(item, app_url, brief_date)

    # Per-docket sections (personalized path)
    for sec in docket_sections:
        ext_id = _esc(str(sec["external_id"]))
        pool_total = sec.get("pool_total", len(sec["items"]))
        filing_word = "filing" if pool_total == 1 else "filings"
        sec_ext_id = sec["external_id"]
        docket_url = f"{app_url}/dockets/{sec_ext_id}?date={brief_date.isoformat()}"
        items_html += (
            f"<div class=\"section-title\">"
            f"DOCKET {ext_id} &mdash; {pool_total} {filing_word} today"
            f"</div>\n"
        )
        for item in sec["items"]:
            items_html += _render_item(item, app_url, brief_date)
        items_html += (
            f"<div style=\"margin:-12px 0 20px\">"
            f"<a href=\"{docket_url}\" style=\"font-size:12px;font-weight:500;"
            f"color:#6366F1;text-decoration:none\">"
            f"View all {pool_total} in dashboard &#x2192;</a>"
            f"</div>\n"
        )

    # Discovery section — entity mentions (no LLM, metadata only)
    if discovery_hits:
        items_html += (
            "<div class=\"section-title\">Mentions of your entities</div>\n"
            "<div style=\"font-size:12px;color:#6B7280;margin:-8px 0 12px;line-height:1.5\">"
            "Surfaced because a name you watch appears in these new filings. "
            "Metadata only — click “Track this” for full analysis."
            "</div>\n"
        )
        for hit in discovery_hits:
            items_html += _render_discovery_hit(hit, app_url)

    # WHAT_CHANGED section (flat/global path)
    wc_items = sections.get("what_changed", [])
    if wc_items:
        _lbl_wc = _esc(_SECTION_LABELS["what_changed"])
        items_html += f"<div class=\"section-title\">{_lbl_wc}</div>\n"
        for item in wc_items:
            items_html += _render_item(item, app_url, brief_date)

    item_word = "item" if item_count == 1 else "items"
    calendar_html = _render_calendar_events(list(calendar_events))

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
    {banner_html}{items_html}{calendar_html}
    <div class="footer">
      <a href="{app_url}/dashboard">View in app</a>
      &nbsp;&middot;&nbsp;
      <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;&middot;&nbsp;
      <a href="https://nodalpulse.com/status">Status</a>
      <span class="footer-stamp">
        v{_esc(composer_version)} &middot; generated {_esc(gen_ct)}
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
    record_url: str = "",
) -> str:
    date_str = brief_date.strftime("%A, %B %-d, %Y")
    filing_word = "filing" if corpus_count == 1 else "filings"
    view_url = record_url or f"{app_url}/dashboard"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>NodalPulse &middot; Quiet day &middot; {_esc(date_str)}</title>
  <style>{_base_styles()}</style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <a href="{app_url}" class="logo">Nodal<span class="logo-pulse">Pulse</span></a>
      <div class="header-meta">{_esc(date_str)}</div>
    </div>
    <div style="padding:24px 0 8px">
      <p style="font-size:15px;font-weight:600;color:#18181B;margin:0 0 8px">Quiet day</p>
      <p style="margin:0;color:#71717A;font-size:14px;line-height:1.55">
        0 items match your filters.
        The full corpus had {corpus_count} {filing_word}.
      </p>
    </div>
    <div class="footer">
      <a href="{view_url}">View filing record</a>
      &nbsp;&middot;&nbsp;
      <a href="{unsubscribe_url}">Unsubscribe</a>
      &nbsp;&middot;&nbsp;
      <a href="https://nodalpulse.com/status">Status</a>
    </div>
  </div>
</body>
</html>"""


def build_maintenance_html(*, brief_date: date, app_url: str, unsubscribe_url: str) -> str:
    date_str = brief_date.strftime("%A, %B %-d, %Y")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>NodalPulse &middot; Pipeline maintenance</title>
  <style>{_base_styles()}</style>
</head>
<body>
  <div class="wrapper">
    <div class="header">
      <a href="{app_url}" class="logo">Nodal<span class="logo-pulse">Pulse</span></a>
      <div class="header-meta">{_esc(date_str)}</div>
    </div>
    <div style="padding:24px 0 8px">
      <p style="font-size:15px;font-weight:600;color:#18181B;margin:0 0 8px">Pipeline maintenance</p>
      <p style="margin:0;color:#71717A;font-size:14px;line-height:1.55">
        Today&rsquo;s brief is paused while the extraction pipeline is under maintenance.
        Check
        <a href="https://nodalpulse.com/status" style="color:#6366F1">nodalpulse.com/status</a>
        for updates.
      </p>
    </div>
    <div class="footer">
      <a href="{app_url}/dashboard">View in app</a>
      &nbsp;&middot;&nbsp;
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
    docket_sections: list[dict] = (),
    app_url: str,
    unsubscribe_url: str,
    composer_version: str,
    discovery_hits: list[dict] = (),
) -> str:
    date_str = brief_date.strftime("%A, %B %d, %Y")
    lines = [f"NodalPulse — {date_str}", "=" * 52, ""]

    tom_items = sections.get("top_of_mind", [])
    if tom_items:
        lines += [f"── {_SECTION_LABELS['top_of_mind'].upper()} ──", ""]
        for item in tom_items:
            lines += [item["title"], item["summary"], item["citation"],
                      f"  {app_url}/filing/{item['filing_id']}", ""]

    for sec in docket_sections:
        lines += [f"── DOCKET {sec['external_id']} ──", ""]
        for item in sec["items"]:
            lines += [item["title"], item["summary"], item["citation"],
                      f"  {app_url}/filing/{item['filing_id']}", ""]
        pool_total = sec.get("pool_total", len(sec["items"]))
        lines += [
            f"  View all {pool_total} filings: "
            f"{app_url}/dockets/{sec['external_id']}?date={brief_date.isoformat()}",
            "",
        ]

    if discovery_hits:
        lines += ["── MENTIONS OF YOUR ENTITIES ──", ""]
        lines += ["  Metadata only — click Track this for full analysis.", ""]
        for hit in discovery_hits:
            first_docket = (hit.get("docket_numbers") or [""])[0]
            filers_raw = (hit.get("filer_names") or [])[:2]
            filers = ", ".join(filers_raw) if filers_raw else "—"
            filed = str(hit.get("filed_at") or "")[:10]
            track_url = (
                f"{app_url}/dockets?q={first_docket}" if first_docket else f"{app_url}/dockets"
            )
            lines += [
                str(hit.get("description") or "Filing"),
                f"  {first_docket} · {filed} · {filers}",
                f"  {track_url}",
                "",
            ]

    wc_items = sections.get("what_changed", [])
    if wc_items:
        lines += [f"── {_SECTION_LABELS['what_changed'].upper()} ──", ""]
        for item in wc_items:
            lines += [item["title"], item["summary"], item["citation"],
                      f"  {app_url}/filing/{item['filing_id']}", ""]

    lines += [
        "-" * 52,
        f"View in app:  {app_url}/dashboard",
        f"Unsubscribe:  {unsubscribe_url}",
        f"Status:       https://nodalpulse.com/status",
        f"v{composer_version}",
    ]
    return "\n".join(lines)
