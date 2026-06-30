"""State-PUC portal accessibility probe (read-only, stdlib-only).

Reusable discovery diagnostic for PJM-Wave-1 (and later) state PUCs. Fetches each
portal's search entry point with a realistic browser UA and reports: HTTP status,
CDN/geo signals, ASP.NET WebForms markers (__VIEWSTATE / __EVENTVALIDATION), and a
one-line verdict. NO writes, NO form submission — purely characterizes access.

WHY THIS EXISTS: the dev sandbox egresses from Colombia, and some portals geo-fence
to the US (VA SCC = CloudFront 403; PA PUC = connection refused) while others only
block by user-agent (MD PSC). Run this from a US egress (Railway) to confirm the
geo-blocked portals' mechanisms before building their adapters:

    railway run python scripts/probe_state_puc.py
    # or locally where reachable:
    python scripts/probe_state_puc.py [va|pa|md|nj|all]
"""

import http.cookiejar
import ssl
import sys
import urllib.error
import urllib.request

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Search entry points discovered during the 2026-06-30 accessibility spike.
PORTALS: dict[str, dict] = {
    "nj": {"name": "NJ BPU", "url": "https://publicaccess.bpu.state.nj.us/Search.aspx"},
    "md": {"name": "MD PSC", "url": "https://webpscxb.pscmaryland.com/DMS/official-filings"},
    "va": {"name": "VA SCC", "url": "https://www.scc.virginia.gov/docketsearch/DocketSearch"},
    "pa": {"name": "PA PUC", "url": "https://www.puc.pa.gov/search/document-search/"},
}


def probe(key: str, spec: dict) -> None:
    print(f"\n=== {spec['name']}  {spec['url']} ===")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    opener.addheaders = [
        ("User-Agent", _UA),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    try:
        with opener.open(spec["url"], timeout=45) as r:
            status = r.status
            headers = dict(r.headers)
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cdn = e.headers.get("X-CDN") or e.headers.get("Server") or "?"
        pop = e.headers.get("X-Amz-Cf-Pop", "")
        verdict = "GEO/WAF block" if e.code == 403 else f"HTTP {e.code}"
        print(f"  status={e.code}  cdn={cdn}  pop={pop}  -> {verdict}")
        return
    except Exception as e:  # noqa: BLE001
        print(f"  UNREACHABLE: {type(e).__name__}: {e}  -> network/geo refusal")
        return

    webforms = "__VIEWSTATE" in html
    axd = ".axd" in html
    has_form = "<form" in html.lower()
    spa = any(m in html.lower() for m in ("react", "ng-app", "__next", "webpack"))
    cdn = headers.get("X-CDN", headers.get("Server", "?"))
    cookies = sorted({c.name.split("_")[0] for c in cj})
    if webforms or axd:
        mech = "ASP.NET WebForms (viewstate/axd) — CpucAdapter-style POST"
    elif spa:
        mech = "JS SPA — may need a JSON backend / browser"
    elif has_form:
        mech = "HTML form (mechanism TBD — inspect fields)"
    else:
        mech = "no form detected — inspect manually"
    print(
        f"  status={status}  bytes={len(html)}  cdn={cdn}  cookies={cookies}\n"
        f"  webforms={webforms} axd={axd} form={has_form} spa={spa}\n"
        f"  -> {mech}"
    )


def main() -> None:
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    keys = list(PORTALS) if arg == "all" else [arg]
    for key in keys:
        if key not in PORTALS:
            print(f"unknown portal '{key}' (choices: {', '.join(PORTALS)}, all)")
            continue
        probe(key, PORTALS[key])


if __name__ == "__main__":
    main()
