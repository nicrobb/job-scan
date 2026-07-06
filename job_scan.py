#!/usr/bin/env python3
"""
job_scan.py  -  Nic's US-tech job scanner.

Polls the public ATS job feeds of target companies, filters for roles that fit
(Solutions Engineer / AI deployment / customer-facing AI), keeps only Australia
or remote-eligible postings, remembers what it has seen, and emails a report
highlighting anything NEW since the last run.

Only dependency: requests  ->  pip install requests
Config (companies, keywords, email) is all near the top of this file.
Secrets come from environment variables - never hard-code them.
"""

import os, json, smtplib, ssl, datetime, traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import requests

# ---------------------------------------------------------------------------
# 1. TARGET COMPANIES  (adapter, ats_token, nice_name)
#    ats_token = the slug in the ATS URL. If a company returns 0 jobs or errors,
#    the run summary tells you, and you just fix the token here.
# ---------------------------------------------------------------------------
COMPANIES = [
    ("greenhouse",     "anthropic",  "Anthropic"),   # verified working
    ("ashby",          "openai",     "OpenAI"),       # verified working
    ("greenhouse",     "gitlab",     "GitLab"),       # verified working
    ("greenhouse",     "databricks", "Databricks"),   # verified working
    ("greenhouse",     "datadog",    "Datadog"),      # verified working
    ("ashby",          "notion",     "Notion"),       # verified working
    ("smartrecruiters","NEXTDC",     "NextDC"),
    # replacements for dead feeds - high-value US tech, remote/AU, E-3 sponsors:
    ("greenhouse",     "stripe",              "Stripe"),
    ("greenhouse",     "snowflake",           "Snowflake"),    # was snowflakecomputing (404)
    ("greenhouse",     "cloudflare",          "Cloudflare"),
    ("ashby",          "mistral",             "Mistral AI"),   # works with includeCompensation=true
    ("ashby",          "zapier",              "Zapier"),       # works (Ashby)
    # Dropped - no working public feed found: GitHub (not on Greenhouse),
    # Canva (not on Greenhouse/Lever). Watch these manually if wanted.
    # --- removed: tokens dead as of last scan (re-add if you find the new one) ---
    #   HubSpot: Greenhouse board 'hubspot' is now empty (moved ATS)
    #   Zapier / Mistral / Canva: returned 404 (token changed)
    #   To fix any feed: open the company's careers page and read the slug from the
    #   URL - boards.greenhouse.io/SLUG, jobs.ashbyhq.com/SLUG, jobs.lever.co/SLUG.
    # --- big US tech via their own JSON feeds (verify on first run) ---
    ("amazon",         "",           "Amazon"),          # confirmed working
    ("workday",        "zoom.wd5.myworkdayjobs.com|zoom|Zoom", "Zoom"),
    # Disabled - endpoints changed: Google (API retired, 404), Microsoft (SSL host issue).
    # ("google",    "", "Google"),
    # ("microsoft", "", "Microsoft"),
    # Salesforce is on Workday too - find the tenant/site in its careers URL, e.g.:
    # ("workday", "salesforce.wd12.myworkdayjobs.com|salesforce|External_Career_Site", "Salesforce"),
    # --- other optional feeds ---
    # ("greenhouse",   "snowflakecomputing", "Snowflake"),
    # ("lever",        "twilio",     "Twilio"),
]

# Queries used for the search-based feeds (Amazon/Google/Microsoft/Workday)
SEARCH_QUERIES = ["solutions architect", "solutions engineer", "ai",
                  "customer success", "applied ai", "deployment"]

# ---------------------------------------------------------------------------
# 2. WHAT COUNTS AS A MATCH
# ---------------------------------------------------------------------------
TITLE_KEYWORDS = [
    "solutions engineer", "solutions architect", "solutions consultant",
    "sales engineer", "customer engineer", "forward deployed",
    "ai deployment", "deployment strategist", "deployment engineer",
    "deployment manager", "applied ai", "ai adoption", "ai enablement",
    "ai consultant", "ai solutions", "ai strategist", "technical account",
    "customer success", "implementation", "onboarding", "value engineer",
    "technical product manager", "product manager, ai",
]

# A posting is kept only if its location text matches one of these
# (blank locations are kept too - often "remote").
LOCATION_KEYWORDS = [
    "australia", "sydney", "melbourne", "brisbane", "perth", "canberra",
    "adelaide", "anz", "apac", "asia pacific", "remote",
]

MIN_AUD = 140000  # for your reference only; ATS feeds rarely include salary

# ---------------------------------------------------------------------------
# 3. EMAIL (all from env vars - set these on the VPS, see README)
# ---------------------------------------------------------------------------
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO", "nicrobb.dc@gmail.com")

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_jobs.json")
HTTP_TIMEOUT = 25
HEADERS = {"User-Agent": "job-scan/1.0 (personal use)"}

# ---------------------------------------------------------------------------
# ATS adapters -> each returns a list of dicts: {id,title,location,url,company}
# ---------------------------------------------------------------------------
def _match(title, location):
    t = (title or "").lower()
    if not any(k in t for k in TITLE_KEYWORDS):
        return False
    loc = (location or "").lower()
    if loc == "":
        return True
    return any(k in loc for k in LOCATION_KEYWORDS)

def fetch_greenhouse(token, name):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if _match(j.get("title"), loc):
            out.append({"id": f"gh-{token}-{j['id']}", "title": j.get("title", ""),
                        "location": loc, "url": j.get("absolute_url", ""), "company": name})
    return out

def fetch_ashby(token, name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location", "") or ", ".join(j.get("secondaryLocations", []) or [])
        if _match(j.get("title"), loc):
            out.append({"id": f"as-{token}-{j.get('jobId') or j.get('id')}",
                        "title": j.get("title", ""), "location": loc,
                        "url": j.get("jobUrl", "") or j.get("applyUrl", ""), "company": name})
    return out

def fetch_lever(token, name):
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        loc = (j.get("categories") or {}).get("location", "")
        if _match(j.get("text"), loc):
            out.append({"id": f"lv-{token}-{j.get('id')}", "title": j.get("text", ""),
                        "location": loc, "url": j.get("hostedUrl", ""), "company": name})
    return out

def fetch_smartrecruiters(token, name):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc_obj = j.get("location", {}) or {}
        loc = ", ".join(x for x in [loc_obj.get("city"), loc_obj.get("country")] if x)
        url_ = f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}"
        if _match(j.get("name"), loc):
            out.append({"id": f"sr-{token}-{j.get('id')}", "title": j.get("name", ""),
                        "location": loc, "url": url_, "company": name})
    return out

def _q(s):
    return requests.utils.quote(s)

def fetch_amazon(token, name):
    out, seen = [], set()
    for q in SEARCH_QUERIES:
        url = (f"https://www.amazon.jobs/en/search.json?base_query={_q(q)}"
               f"&loc_query=Australia&result_limit=50")
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            loc = j.get("normalized_location", "")
            jid = j.get("id_icims") or j.get("id")
            if jid not in seen and _match(j.get("title"), loc):
                seen.add(jid)
                out.append({"id": f"amz-{jid}", "title": j.get("title", ""),
                            "location": loc,
                            "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                            "company": name})
    return out

def fetch_google(token, name):
    out, seen = [], set()
    for q in SEARCH_QUERIES:
        url = (f"https://careers.google.com/api/v3/search/?q={_q(q)}"
               f"&location=Australia&page_size=50")
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            locs = ", ".join(l.get("display", "") for l in j.get("locations", []))
            jid = (j.get("id") or "").split("/")[-1]
            if jid not in seen and _match(j.get("title"), locs):
                seen.add(jid)
                out.append({"id": f"goog-{jid}", "title": j.get("title", ""),
                            "location": locs,
                            "url": f"https://www.google.com/about/careers/applications/jobs/results/{jid}",
                            "company": name})
    return out

def fetch_microsoft(token, name):
    hdr = dict(HEADERS); hdr["Accept"] = "application/json"
    out, seen = [], set()
    for q in SEARCH_QUERIES:
        url = (f"https://gcsservices.careers.microsoft.com/search/api/v1/search?"
               f"q={_q(q)}&lc=Australia&pg=1&pgSz=50&o=Relevance&flt=true")
        r = requests.get(url, headers=hdr, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = (r.json().get("operationResult", {}) or {}).get("result", {}) or {}
        for j in data.get("jobs", []):
            props = j.get("properties", {}) or {}
            loc = ", ".join(props.get("locations", []) or [props.get("primaryLocation", "")])
            jid = j.get("jobId", "")
            if jid not in seen and _match(j.get("title"), loc):
                seen.add(jid)
                out.append({"id": f"msft-{jid}", "title": j.get("title", ""),
                            "location": loc,
                            "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                            "company": name})
    return out

def fetch_workday(token, name):
    host, tenant, site = token.split("|")
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    hdr = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}
    out, seen = [], set()
    for q in SEARCH_QUERIES:
        body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}
        r = requests.post(api, json=body, headers=hdr, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            loc = j.get("locationsText", "")
            path = j.get("externalPath", "")
            if path not in seen and _match(j.get("title"), loc):
                seen.add(path)
                out.append({"id": f"wd-{tenant}-{path}", "title": j.get("title", ""),
                            "location": loc, "url": f"https://{host}{path}", "company": name})
    return out

ADAPTERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby,
            "lever": fetch_lever, "smartrecruiters": fetch_smartrecruiters,
            "amazon": fetch_amazon, "google": fetch_google,
            "microsoft": fetch_microsoft, "workday": fetch_workday}

# ---------------------------------------------------------------------------
def load_seen():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(ids):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=0)

def build_html(all_jobs, new_ids, errors):
    today = datetime.date.today().isoformat()
    new_jobs = [j for j in all_jobs if j["id"] in new_ids]
    def row(j, star=False):
        s = "⭐ " if star else ""
        return (f'<tr><td style="padding:6px 10px">{s}<b>{j["title"]}</b></td>'
                f'<td style="padding:6px 10px">{j["company"]}</td>'
                f'<td style="padding:6px 10px">{j["location"] or "-"}</td>'
                f'<td style="padding:6px 10px"><a href="{j["url"]}">view</a></td></tr>')
    html = [f"<h2>Job scan - {today}</h2>",
            f"<p><b>{len(new_jobs)} new</b> matching role(s) since last run &middot; "
            f"{len(all_jobs)} total matches tracked.</p>"]
    if new_jobs:
        html.append("<h3>⭐ New since last run</h3><table style='border-collapse:collapse'>")
        html += [row(j, True) for j in sorted(new_jobs, key=lambda x: x["company"])]
        html.append("</table>")
    html.append("<h3>All current matches</h3><table style='border-collapse:collapse'>")
    html += [row(j) for j in sorted(all_jobs, key=lambda x: (x["company"], x["title"]))]
    html.append("</table>")
    if errors:
        html.append("<h3>Feeds that failed (check the token in COMPANIES)</h3><ul>")
        html += [f"<li>{c}: {e}</li>" for c, e in errors]
        html.append("</ul>")
    html.append(f"<p style='color:#888'>Filter: AU/remote + role keywords. Target base &ge; A${MIN_AUD:,}.</p>")
    return "\n".join(html)

def send_email(subject, html):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        print("[warn] SMTP env vars not set - printing report instead of emailing.\n")
        print(html)
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls(context=ctx)
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print(f"[ok] emailed report to {EMAIL_TO}")

DASHBOARD_TEMPLATE = r"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nic's Job Tracker</title><style>
:root{--navy:#1f3a5f;--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--muted:#667;--green:#1e7a34;--red:#b00020;}
*{box-sizing:border-box}body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:#1a1a1a}
header{background:var(--navy);color:#fff;padding:18px 22px}header h1{margin:0;font-size:20px}header p{margin:4px 0 0;font-size:13px;opacity:.85}
.wrap{max-width:940px;margin:0 auto;padding:18px 16px 60px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 8px}
.bar button{border:1px solid var(--line);background:#fff;border-radius:20px;padding:6px 12px;font-size:13px;cursor:pointer}
.bar button.active{background:var(--navy);color:#fff;border-color:var(--navy)}
.counts{margin-left:auto;font-size:13px;color:var(--muted)}
.group h2{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--navy);border-bottom:2px solid var(--navy);padding-bottom:5px;margin:18px 0 8px}
.job{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px;display:flex;gap:12px;align-items:flex-start}
.job.done{opacity:.5}.job .main{flex:1;min-width:0}.job .title{font-weight:600}
.job .meta{font-size:12.5px;color:var(--muted);margin-top:2px}
.job a.apply{font-size:13px;text-decoration:none;color:var(--navy);font-weight:600}
.acts{display:flex;gap:6px;flex-shrink:0}
.acts button{border:1px solid var(--line);background:#fff;border-radius:7px;padding:6px 9px;font-size:12.5px;cursor:pointer;white-space:nowrap}
.acts button.appliedOn{background:var(--green);color:#fff;border-color:var(--green)}
.acts button.unsuitOn{background:var(--red);color:#fff;border-color:var(--red)}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;background:#eef2f7;color:var(--navy);margin-left:6px}
footer{max-width:940px;margin:0 auto;padding:0 16px 40px;font-size:12px;color:var(--muted)}
</style></head><body>
<header><h1>Nic's Job Tracker</h1><p>Updated __DATE__ &middot; __SUMMARY__ &middot; tick Applied / Not suitable as you go (saved in this browser).</p></header>
<div class="wrap"><div class="bar">
<button data-f="review" class="active">To review</button><button data-f="applied">Applied</button>
<button data-f="unsuitable">Not suitable</button><button data-f="all">All</button>
<span class="counts" id="counts"></span></div><div id="list"></div></div>
<footer>Auto-generated weekly. Your ticks are stored in this browser and keyed by job link, so they persist as the list refreshes.</footer>
<script>
const JOBS=__JOBS__;const KEY="nic_job_status_v1";
const load=()=>{try{return JSON.parse(localStorage.getItem(KEY))||{}}catch(e){return{}}};
const save=s=>localStorage.setItem(KEY,JSON.stringify(s));
let status=load(),filter="review";
function setStatus(u,v){if(status[u]===v)delete status[u];else status[u]=v;save(status);render();}
function render(){const list=document.getElementById("list");list.innerHTML="";
 const vis=JOBS.filter(j=>{const s=status[j.u];if(filter==="all")return true;if(filter==="applied")return s==="applied";if(filter==="unsuitable")return s==="unsuitable";return !s;});
 const groups={};vis.forEach(j=>{(groups[j.g]=groups[j.g]||[]).push(j)});
 Object.keys(groups).forEach(g=>{const gd=document.createElement("div");gd.className="group";gd.innerHTML="<h2>"+g+"</h2>";
  groups[g].forEach(j=>{const s=status[j.u];const el=document.createElement("div");el.className="job"+(s?" done":"");
   el.innerHTML='<div class="main"><div class="title">'+j.t+'<span class="tag">'+j.c+'</span><span class="tag">'+j.l+'</span></div>'+
   '<div class="meta"><a class="apply" href="'+j.u+'" target="_blank" rel="noopener">Open posting ↗</a></div></div>'+
   '<div class="acts"><button class="'+(s==="applied"?"appliedOn":"")+'" data-u="'+j.u+'" data-v="applied">✅ Applied</button>'+
   '<button class="'+(s==="unsuitable"?"unsuitOn":"")+'" data-u="'+j.u+'" data-v="unsuitable">\u{1F6AB} Not suitable</button></div>';
   gd.appendChild(el);});list.appendChild(gd);});
 const ap=JOBS.filter(j=>status[j.u]==="applied").length,un=JOBS.filter(j=>status[j.u]==="unsuitable").length,rv=JOBS.length-ap-un;
 document.getElementById("counts").textContent=rv+" to review · "+ap+" applied · "+un+" passed · "+JOBS.length+" total";
 if(!vis.length)list.innerHTML='<p style="color:#667;padding:20px 0">Nothing here — switch filter above.</p>';}
document.getElementById("list").addEventListener("click",e=>{const b=e.target.closest("button[data-u]");if(b)setStatus(b.dataset.u,b.dataset.v);});
document.querySelectorAll(".bar button[data-f]").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.f;document.querySelectorAll(".bar button[data-f]").forEach(x=>x.classList.remove("active"));b.classList.add("active");render();}));
render();
</script></body></html>"""

def _region(loc):
    l = (loc or "").lower()
    au = ["australia","sydney","melbourne","brisbane","perth","canberra","adelaide","anz","apac","apj"]
    if any(k in l for k in au):
        return (0, "\U0001F1E6\U0001F1FA Australia / ANZ")
    if "remote" in l and any(k in l for k in ["united states","u.s","usa","us-","us ","americas","america","san francisco","new york","seattle"]):
        return (1, "\U0001F30E Remote - US")
    if "remote" in l:
        return (2, "\U0001F310 Remote - other")
    return (3, "\U0001F4CD " + (loc or "Other"))

def write_dashboard(all_jobs):
    rows = []
    for j in all_jobs:
        w, g = _region(j["location"])
        rows.append({"w": w, "g": g, "t": j["title"], "c": j["company"],
                     "l": j["location"] or "Remote", "u": j["url"]})
    rows.sort(key=lambda r: (r["w"], r["c"].lower(), r["t"].lower()))
    today = datetime.date.today().isoformat()
    html = (DASHBOARD_TEMPLATE
            .replace("__JOBS__", json.dumps(rows))
            .replace("__DATE__", today)
            .replace("__SUMMARY__", f"{len(rows)} roles tracked"))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    with open(out, "w") as f:
        f.write(html)
    print(f"[ok] wrote dashboard {out}")

def main():
    all_jobs, errors = [], []
    for adapter, token, name in COMPANIES:
        try:
            jobs = ADAPTERS[adapter](token, name)
            all_jobs.extend(jobs)
            print(f"[ok] {name}: {len(jobs)} match(es)")
        except Exception as e:
            errors.append((name, str(e)))
            print(f"[err] {name}: {e}")
    # dedupe
    uniq = {j["id"]: j for j in all_jobs}
    all_jobs = list(uniq.values())
    seen = load_seen()
    current_ids = set(uniq.keys())
    new_ids = current_ids - seen
    # always regenerate the hosted dashboard
    write_dashboard(all_jobs)
    # email is optional now - only sends if SMTP env vars are set
    if SMTP_HOST and SMTP_USER and SMTP_PASS:
        html = build_html(all_jobs, new_ids, errors)
        subject = (f"Job scan: {len(new_ids)} new role(s)" if new_ids
                   else "Job scan: no new roles today")
        send_email(subject, html)
    else:
        print(f"[info] {len(new_ids)} new / {len(all_jobs)} total. "
              f"Dashboard written; SMTP not set so no email sent.")
    save_seen(seen | current_ids)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
