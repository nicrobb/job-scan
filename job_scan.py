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
    ("greenhouse",     "cloudflare",          "Cloudflare"),
    # Snowflake dropped: on the Phenom People platform, no simple public feed.
    ("ashby_gql",      "mistral",             "Mistral AI"),   # internal Ashby GraphQL
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
    ("microsoft",      "",           "Microsoft"),        # new apply.careers.microsoft.com API
    ("workday",        "zoom.wd5.myworkdayjobs.com|zoom|Zoom", "Zoom"),
    # Google disabled - careers moved to an obfuscated internal RPC (no clean feed).
    # ("google",    "", "Google"),
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
    # plural and singular "solution(s)" variants (Microsoft uses singular)
    "solutions engineer", "solution engineer", "solutions architect",
    "solution architect", "solutions consultant", "solution consultant",
    "solution specialist", "solution area", "cloud solution", "solution sales",
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
    out, seen = [], set()
    for q in SEARCH_QUERIES:
        url = (f"https://apply.careers.microsoft.com/api/pcsx/search?q={_q(q)}"
               f"&location=Australia&domain=microsoft.com&hl=en&pg=1&pgSz=50")
        r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        for j in (r.json().get("data", {}) or {}).get("positions", []):
            loc = ", ".join(j.get("locations", []) or [])
            jid = j.get("id")
            if jid not in seen and _match(j.get("name"), loc):
                seen.add(jid)
                out.append({"id": f"msft-{jid}", "title": j.get("name", ""),
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

def fetch_ashby_gql(token, name):
    """Ashby internal GraphQL - works when the public posting API is disabled."""
    url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    query = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) "
             "{ jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: "
             "$organizationHostedJobsPageName) { jobPostings { id title locationName "
             "secondaryLocations { locationName } } } }")
    body = {"operationName": "ApiJobBoardWithTeams",
            "variables": {"organizationHostedJobsPageName": token}, "query": query}
    hdr = {**HEADERS, "Content-Type": "application/json", "Accept": "application/json"}
    r = requests.post(url, json=body, headers=hdr, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    board = ((r.json().get("data", {}) or {}).get("jobBoard", {}) or {})
    out = []
    for j in board.get("jobPostings", []):
        locs = [j.get("locationName", "")] + [s.get("locationName", "") for s in (j.get("secondaryLocations") or [])]
        loc = ", ".join(x for x in locs if x)
        if _match(j.get("title"), loc):
            out.append({"id": f"agql-{token}-{j.get('id')}", "title": j.get("title", ""),
                        "location": loc, "url": f"https://jobs.ashbyhq.com/{token}/{j.get('id')}",
                        "company": name})
    return out

ADAPTERS = {"greenhouse": fetch_greenhouse, "ashby": fetch_ashby,
            "lever": fetch_lever, "smartrecruiters": fetch_smartrecruiters,
            "amazon": fetch_amazon, "google": fetch_google,
            "microsoft": fetch_microsoft, "workday": fetch_workday,
            "ashby_gql": fetch_ashby_gql}

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
<button id="sortBtn" style="margin-left:10px">&#11088; Sort: Best fit</button>
<span class="counts" id="counts"></span></div>
<div class="bar" style="margin-top:0"><span style="font-size:12px;color:#667;align-self:center;margin-right:2px">Location:</span><button data-loc="any" class="active">All</button><button data-loc="vic">Melbourne / Geelong</button><button data-loc="remote">Remote</button><button data-loc="remoteus">Remote &mdash; US ($USD)</button></div>
<div id="list"></div></div>
<div class="wrap" style="padding-top:0"><div class="group">
<h2>Check manually (can't auto-scan)</h2>
<p style="font-size:14px;color:#555;margin:0 0 8px">These sit on platforms the scanner can't read. Bookmark them, or set a LinkedIn job alert so they email you.</p>
<p style="font-size:15px;line-height:2">
&#128269; <a href="https://www.google.com/about/careers/applications/jobs/results/?location=Australia&amp;q=solutions%20architect" target="_blank" rel="noopener">Google Careers &mdash; Australia search &#8599;</a><br>
&#128269; <a href="https://careers.snowflake.com/us/en/search-results?keywords=solutions%20architect" target="_blank" rel="noopener">Snowflake Careers &#8599;</a> <span style="color:#888">(set Location = Australia)</span><br>
&#128269; <a href="https://www.lifeatcanva.com/en/jobs/" target="_blank" rel="noopener">Canva jobs &#8599;</a> &middot; <a href="https://www.linkedin.com/company/canva/jobs/" target="_blank" rel="noopener">Canva on LinkedIn &#8599;</a>
</p></div></div>
<footer>Auto-generated weekly. Your ticks are stored in this browser and keyed by job link, so they persist as the list refreshes.</footer>
<script>
const JOBS=__JOBS__;const KEY="nic_job_status_v1";
const load=()=>{try{return JSON.parse(localStorage.getItem(KEY))||{}}catch(e){return{}}};
const save=s=>localStorage.setItem(KEY,JSON.stringify(s));
let status=load(),filter="review",sortMode="region",locFilter="any";
function setStatus(u,v){if(status[u]===v)delete status[u];else status[u]=v;save(status);render();}
function fitScore(j){var t=(j.t||"").toLowerCase(),g=j.g||"",s=0;
 // location: on-the-ground AU first
 if(g.indexOf("Australia")>-1)s+=40;else if(g.indexOf("Remote - US")>-1)s+=20;else if(g.indexOf("Remote - other")>-1)s+=10;else s+=4;
 // role fit - "best" = winnable for an operator + hands-on AI builder (lower experience gate)
 var best=["customer success","adoption","enablement","ai consultant","solutions consultant","solution consultant","onboarding","implementation","associate","customer engineer","deployment strategist"];
 var okc=["solutions engineer","solution engineer","sales engineer","technical account","solution specialist","solution area","cloud solution","applied ai","ai solution","ai deployment","solutions architect","solution architect","deployment engineer"];
 if(best.some(function(k){return t.indexOf(k)>-1}))s+=32;else if(okc.some(function(k){return t.indexOf(k)>-1}))s+=16;else s+=4;
 if(/adoption|enablement|applied ai|\bai\b/.test(t))s+=6;
 // penalise roles that gate on experience you don't have yet
 var icMgr=/customer success manager|account manager|product manager|program manager|technical account/.test(t);
 if(/\bhead of\b|\bdirector\b|vice president|\bvp\b/.test(t))s-=40;
 if(/\bprincipal\b|\bstaff\b/.test(t))s-=22;
 if(!icMgr&&/\bmanager\b|managing\b/.test(t))s-=18;   // people-leadership
 if(/\blead\b|\bleader\b/.test(t))s-=10;
 if(/software engineer|ml engineer|machine learning|data engineer|security engineer/.test(t))s-=22;
 if(/forward deployed engineer/.test(t))s-=10;
 if(/\bsenior\b|\bsr\b/.test(t))s-=6;
 // small nudge for E-3 sponsors (US-move path)
 var big=["Amazon","Microsoft","OpenAI","Anthropic","Google","Databricks"];
 if(big.indexOf(j.c)>-1)s+=3;
 return s;}
function jobEl(j,rank){var s=status[j.u];var el=document.createElement("div");el.className="job"+(s?" done":"");
 var pre=rank?'<span class="tag" style="background:#1f3a5f;color:#fff">#'+rank+'</span> ':'';
 el.innerHTML='<div class="main"><div class="title">'+pre+j.t+'<span class="tag">'+j.c+'</span><span class="tag">'+j.l+'</span></div>'+
 '<div class="meta"><a class="apply" href="'+j.u+'" target="_blank" rel="noopener">Open posting ↗</a></div></div>'+
 '<div class="acts"><button class="'+(s==="applied"?"appliedOn":"")+'" data-u="'+j.u+'" data-v="applied">✅ Applied</button>'+
 '<button class="'+(s==="unsuitable"?"unsuitOn":"")+'" data-u="'+j.u+'" data-v="unsuitable">\u{1F6AB} Not suitable</button></div>';return el;}
function locMatch(j){var l=(j.l||"").toLowerCase(),g=j.g||"";if(locFilter==="any")return true;if(locFilter==="vic")return /melbourne|geelong|victoria|\bvic\b/.test(l);if(locFilter==="remote")return g.indexOf("Remote")>-1||l.indexOf("remote")>-1;if(locFilter==="remoteus")return g.indexOf("Remote - US")>-1;return true;}
function render(){var list=document.getElementById("list");list.innerHTML="";
 var vis=JOBS.filter(function(j){if(!locMatch(j))return false;var s=status[j.u];if(filter==="all")return true;if(filter==="applied")return s==="applied";if(filter==="unsuitable")return s==="unsuitable";return !s;});
 if(sortMode==="fit"){var ranked=vis.slice().sort(function(a,b){return fitScore(b)-fitScore(a)});
  var gd=document.createElement("div");gd.className="group";gd.innerHTML='<h2>⭐ Best fit for you (realistic + winnable first)</h2>';
  ranked.forEach(function(j,i){gd.appendChild(jobEl(j,i+1))});list.appendChild(gd);
 }else{var groups={};vis.forEach(function(j){(groups[j.g]=groups[j.g]||[]).push(j)});
  Object.keys(groups).forEach(function(g){var gd=document.createElement("div");gd.className="group";gd.innerHTML="<h2>"+g+"</h2>";
   groups[g].forEach(function(j){gd.appendChild(jobEl(j))});list.appendChild(gd);});}
 var ap=JOBS.filter(function(j){return status[j.u]==="applied"}).length,un=JOBS.filter(function(j){return status[j.u]==="unsuitable"}).length,rv=JOBS.length-ap-un;
 document.getElementById("counts").textContent=rv+" to review · "+ap+" applied · "+un+" passed · "+JOBS.length+" total";
 if(!vis.length)list.innerHTML='<p style="color:#667;padding:20px 0">Nothing here — switch filter above.</p>';}
document.getElementById("list").addEventListener("click",e=>{const b=e.target.closest("button[data-u]");if(b)setStatus(b.dataset.u,b.dataset.v);});
document.querySelectorAll(".bar button[data-f]").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.f;document.querySelectorAll(".bar button[data-f]").forEach(x=>x.classList.remove("active"));b.classList.add("active");render();}));
document.querySelectorAll(".bar button[data-loc]").forEach(b=>b.addEventListener("click",()=>{locFilter=b.dataset.loc;document.querySelectorAll(".bar button[data-loc]").forEach(x=>x.classList.remove("active"));b.classList.add("active");render();}));
document.getElementById("sortBtn").addEventListener("click",function(){sortMode=sortMode==="fit"?"region":"fit";this.textContent=sortMode==="fit"?"↩ Sort: By region":"⭐ Sort: Best fit";this.classList.toggle("active",sortMode==="fit");render();});
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
