"""
SafeKeep — Post-Occupancy Essential Safety Measures (ESM) Compliance Tracker
============================================================================
A functional model for tracking ongoing fire-safety / essential-safety-measure
compliance of existing buildings, localised to Australian requirements
(NSW Annual Fire Safety Statement model + generic ESM schedule).

Stack: Flask + SQLite, single file, server-rendered. No build step.
Run:   python app.py    then open http://127.0.0.1:5000

Author: (assignment build)
"""

import sqlite3
import os
from datetime import datetime, date, timedelta
from flask import (
    Flask, g, request, redirect, url_for, render_template_string, flash
)
import ai_extract  # AI component: extracts structured data from inspection reports

APP_DB = os.path.join(os.path.dirname(__file__), "safekeep.db")
app = Flask(__name__)
app.secret_key = "assignment-demo-key"

# ---------------------------------------------------------------------------
# A catalogue of common Essential Safety Measures and how often each must be
# inspected/serviced under typical AU requirements (AS 1851 servicing + NCC).
# Intervals are in months. This is the "domain knowledge" the app reasons over.
# ---------------------------------------------------------------------------
ESM_CATALOGUE = [
    ("Fire detection & alarm system",        12, "AS 1851"),
    ("Emergency lighting & exit signs",       6, "AS 2293.2"),
    ("Fire hydrant system",                  12, "AS 1851"),
    ("Fire hose reels",                       6, "AS 1851"),
    ("Portable fire extinguishers",           6, "AS 1851"),
    ("Automatic fire sprinkler system",      12, "AS 1851"),
    ("Fire doors & fire-resistant exits",    12, "AS 1851"),
    ("Mechanical air handling / smoke ctrl", 12, "AS 1851"),
    ("Path of travel to exits",              12, "NCC / EP&A"),
    ("Emergency warning & intercom system",  12, "AS 1851"),
]

RISK_WINDOW_DAYS = 30  # how many days before due that we flag "due soon"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(APP_DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(APP_DB)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS building (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            address       TEXT NOT NULL,
            classification TEXT,            -- NCC building class e.g. Class 2, 5, 9b
            owner_contact TEXT,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS measure (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            building_id   INTEGER NOT NULL,
            name          TEXT NOT NULL,
            standard      TEXT,
            interval_months INTEGER NOT NULL,
            last_inspected TEXT,            -- ISO date or NULL if never
            FOREIGN KEY (building_id) REFERENCES building(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS inspection (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            measure_id    INTEGER NOT NULL,
            inspected_on  TEXT NOT NULL,
            inspector     TEXT,
            result        TEXT NOT NULL,    -- Pass / Fail / Defect noted
            notes         TEXT,
            FOREIGN KEY (measure_id) REFERENCES measure(id) ON DELETE CASCADE
        );
        """
    )
    db.commit()
    db.close()


def seed_db():
    """Insert a couple of demo buildings so the app isn't empty on first run."""
    db = sqlite3.connect(APP_DB)
    db.row_factory = sqlite3.Row
    existing = db.execute("SELECT COUNT(*) c FROM building").fetchone()["c"]
    if existing:
        db.close()
        return

    now = datetime.now().isoformat(timespec="seconds")
    buildings = [
        ("Collins Street Tower", "350 Collins St, Melbourne VIC 3000",
         "Class 5 — Office", "facilities@collinstower.com.au"),
        ("Brunswick Apartments", "12 Sydney Rd, Brunswick VIC 3056",
         "Class 2 — Residential", "strata@brunswickapts.com.au"),
    ]
    for name, addr, cls, contact in buildings:
        cur = db.execute(
            "INSERT INTO building (name,address,classification,owner_contact,created_at)"
            " VALUES (?,?,?,?,?)", (name, addr, cls, contact, now))
        bid = cur.lastrowid
        # Give each building the full ESM catalogue, with varied last-inspected
        # dates so the dashboard shows a realistic mix of statuses.
        offsets = [2, 7, 13, 5, 14, 1, 8, 11, 4, 20]  # months ago
        for (mname, interval, std), off in zip(ESM_CATALOGUE, offsets):
            last = (date.today() - timedelta(days=off * 30)).isoformat()
            db.execute(
                "INSERT INTO measure "
                "(building_id,name,standard,interval_months,last_inspected)"
                " VALUES (?,?,?,?,?)", (bid, mname, std, interval, last))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Compliance logic — the core "reasoning" of the app
# ---------------------------------------------------------------------------
def compute_status(last_inspected, interval_months):
    """Return (status, due_date, days_left) for a measure.
    status in {'compliant','due_soon','overdue','never'}."""
    if not last_inspected:
        return ("never", None, None)
    last = date.fromisoformat(last_inspected)
    # approx month math: 30-day months is fine for a functional model
    due = last + timedelta(days=interval_months * 30)
    days_left = (due - date.today()).days
    if days_left < 0:
        status = "overdue"
    elif days_left <= RISK_WINDOW_DAYS:
        status = "due_soon"
    else:
        status = "compliant"
    return (status, due.isoformat(), days_left)


def building_summary(bid):
    """Aggregate compliance status for one building."""
    db = get_db()
    measures = db.execute(
        "SELECT * FROM measure WHERE building_id=?", (bid,)).fetchall()
    counts = {"compliant": 0, "due_soon": 0, "overdue": 0, "never": 0}
    enriched = []
    for m in measures:
        status, due, days = compute_status(m["last_inspected"], m["interval_months"])
        counts[status] += 1
        enriched.append({**dict(m), "status": status, "due": due, "days": days})
    total = len(measures) or 1
    # Overall building health: overdue/never drag it down hardest
    score = round(100 * (counts["compliant"] + 0.5 * counts["due_soon"]) / total)
    if counts["overdue"] or counts["never"]:
        overall = "Non-compliant"
    elif counts["due_soon"]:
        overall = "Action needed"
    else:
        overall = "Compliant"
    return enriched, counts, score, overall


# ---------------------------------------------------------------------------
# Templates (embedded). Base layout + pages.
# ---------------------------------------------------------------------------
BASE = """
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>SafeKeep — ESM Compliance</title>
<style>
 :root{--bg:#0e1116;--card:#171c24;--line:#262d39;--ink:#e6e9ef;--mut:#8a94a6;
   --ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--never:#7f8c8d;--acc:#4c8bf5}
 *{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;
   background:var(--bg);color:var(--ink)}
 a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
 header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 22px;
   display:flex;align-items:center;gap:14px}
 header h1{font-size:18px;margin:0;letter-spacing:.5px}
 header .tag{color:var(--mut);font-size:12px}
 nav{margin-left:auto;display:flex;gap:18px;font-size:14px}
 main{max-width:980px;margin:0 auto;padding:24px 22px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:18px 20px;margin-bottom:18px}
 h2{font-size:16px;margin:0 0 12px}h3{margin:.2em 0}
 table{width:100%;border-collapse:collapse}th,td{padding:9px 10px;text-align:left;
   border-bottom:1px solid var(--line);font-size:14px}th{color:var(--mut);font-weight:600}
 .pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}
 .compliant{background:rgba(46,204,113,.15);color:var(--ok)}
 .due_soon{background:rgba(241,196,15,.15);color:var(--warn)}
 .overdue{background:rgba(231,76,60,.15);color:var(--bad)}
 .never{background:rgba(127,140,141,.2);color:#aeb6c2}
 .btn{display:inline-block;background:var(--acc);color:#fff;border:0;border-radius:7px;
   padding:8px 14px;font-size:14px;cursor:pointer}
 .btn.sm{padding:4px 10px;font-size:12px}.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--ink)}
 .btn.danger{background:var(--bad)}
 input,select,textarea{background:#0f141b;border:1px solid var(--line);color:var(--ink);
   border-radius:7px;padding:8px 10px;font:inherit;width:100%}
 label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
 .row{display:flex;gap:14px;flex-wrap:wrap}.row>div{flex:1;min-width:160px}
 .bar{height:8px;background:#0f141b;border-radius:6px;overflow:hidden;margin-top:6px}
 .bar>i{display:block;height:100%;background:var(--ok)}
 .flash{background:rgba(76,139,245,.15);border:1px solid var(--acc);padding:10px 14px;
   border-radius:8px;margin-bottom:16px;font-size:14px}
 .muted{color:var(--mut)}.big{font-size:30px;font-weight:700}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
 .stat{background:#0f141b;border:1px solid var(--line);border-radius:9px;padding:14px}
 .stat .n{font-size:24px;font-weight:700}.stat .l{font-size:12px;color:var(--mut)}
</style></head><body>
<header>
  <h1>🛡 SafeKeep</h1><span class=tag>Post-Occupancy ESM Compliance · AU</span>
  <nav><a href="{{ url_for('dashboard') }}">Dashboard</a>
       <a href="{{ url_for('buildings') }}">Buildings</a>
       <a href="{{ url_for('add_building') }}">+ New building</a></nav>
</header>
<main>
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}
    <div class=flash>{{ m }}</div>{% endfor %}{% endwith %}
  {{ body|safe }}
</main></body></html>
"""


def page(body_html, **ctx):
    inner = render_template_string(body_html, **ctx)
    return render_template_string(BASE, body=inner)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    db = get_db()
    buildings = db.execute("SELECT * FROM building ORDER BY name").fetchall()
    rows, totals = [], {"compliant": 0, "due_soon": 0, "overdue": 0, "never": 0}
    for b in buildings:
        _, counts, score, overall = building_summary(b["id"])
        for k in totals:
            totals[k] += counts[k]
        rows.append({"b": b, "counts": counts, "score": score, "overall": overall})
    body = """
    <h2>Portfolio compliance overview</h2>
    <div class=grid style="margin-bottom:18px">
      <div class=stat><div class=n style="color:var(--ok)">{{t.compliant}}</div>
         <div class=l>Measures compliant</div></div>
      <div class=stat><div class=n style="color:var(--warn)">{{t.due_soon}}</div>
         <div class=l>Due within 30 days</div></div>
      <div class=stat><div class=n style="color:var(--bad)">{{t.overdue}}</div>
         <div class=l>Overdue</div></div>
      <div class=stat><div class=n style="color:#aeb6c2">{{t.never}}</div>
         <div class=l>Never inspected</div></div>
    </div>
    <div class=card><h2>Buildings</h2>
    <table><tr><th>Building</th><th>Class</th><th>Health</th>
      <th>Overdue</th><th>Due soon</th><th>Status</th><th></th></tr>
    {% for r in rows %}
      <tr>
        <td><a href="{{ url_for('view_building', bid=r.b['id']) }}">{{ r.b['name'] }}</a>
            <div class=muted style="font-size:12px">{{ r.b['address'] }}</div></td>
        <td class=muted>{{ r.b['classification'] }}</td>
        <td style="width:130px">{{ r.score }}%
            <div class=bar><i style="width:{{ r.score }}%;
               background:{% if r.score>=80 %}var(--ok){% elif r.score>=50 %}var(--warn){% else %}var(--bad){% endif %}"></i></div></td>
        <td>{% if r.counts['overdue'] %}<span class="pill overdue">{{ r.counts['overdue'] }}</span>{% else %}0{% endif %}</td>
        <td>{% if r.counts['due_soon'] %}<span class="pill due_soon">{{ r.counts['due_soon'] }}</span>{% else %}0{% endif %}</td>
        <td><span class="pill {{ 'overdue' if r.overall=='Non-compliant' else 'due_soon' if r.overall=='Action needed' else 'compliant' }}">{{ r.overall }}</span></td>
        <td><a class="btn sm ghost" href="{{ url_for('view_building', bid=r.b['id']) }}">Open</a></td>
      </tr>
    {% endfor %}
    {% if not rows %}<tr><td colspan=7 class=muted>No buildings yet. <a href="{{ url_for('add_building') }}">Add one</a>.</td></tr>{% endif %}
    </table></div>
    """
    return page(body, rows=rows, t=totals)


@app.route("/buildings")
def buildings():
    db = get_db()
    bs = db.execute("SELECT * FROM building ORDER BY name").fetchall()
    body = """
    <h2>All buildings</h2><div class=card>
    <table><tr><th>Name</th><th>Address</th><th>Class</th><th>Contact</th><th></th></tr>
    {% for b in bs %}<tr>
      <td><a href="{{ url_for('view_building', bid=b['id']) }}">{{ b['name'] }}</a></td>
      <td class=muted>{{ b['address'] }}</td><td class=muted>{{ b['classification'] }}</td>
      <td class=muted>{{ b['owner_contact'] }}</td>
      <td><a class="btn sm ghost" href="{{ url_for('edit_building', bid=b['id']) }}">Edit</a></td>
    </tr>{% endfor %}</table></div>
    <a class=btn href="{{ url_for('add_building') }}">+ New building</a>
    """
    return page(body, bs=bs)


@app.route("/building/<int:bid>")
def view_building(bid):
    db = get_db()
    b = db.execute("SELECT * FROM building WHERE id=?", (bid,)).fetchone()
    if not b:
        flash("Building not found.")
        return redirect(url_for("dashboard"))
    measures, counts, score, overall = building_summary(bid)
    # sort: overdue first, then due_soon, then never, then compliant
    order = {"overdue": 0, "due_soon": 1, "never": 2, "compliant": 3}
    measures.sort(key=lambda m: order[m["status"]])
    body = """
    <p><a href="{{ url_for('dashboard') }}">← Dashboard</a></p>
    <div class=card>
      <div class=row>
        <div><h2 style="margin:0">{{ b['name'] }}</h2>
          <div class=muted>{{ b['address'] }}</div>
          <div class=muted>{{ b['classification'] }} · {{ b['owner_contact'] }}</div></div>
        <div style="flex:0 0 auto;text-align:right">
          <div class=big>{{ score }}%</div>
          <span class="pill {{ 'overdue' if overall=='Non-compliant' else 'due_soon' if overall=='Action needed' else 'compliant' }}">{{ overall }}</span>
        </div>
      </div>
      <p style="margin-top:14px">
        <a class="btn sm" href="{{ url_for('edit_building', bid=b['id']) }}">Edit building</a>
        <a class="btn sm" href="{{ url_for('add_measure', bid=b['id']) }}">+ Add measure</a>
        <a class="btn sm ghost" href="{{ url_for('statement', bid=b['id']) }}">Generate annual statement</a>
      </p>
    </div>

    <div class=card><h2>Essential Safety Measures ({{ measures|length }})</h2>
    <table><tr><th>Measure</th><th>Standard</th><th>Interval</th>
       <th>Last inspected</th><th>Next due</th><th>Status</th><th>Actions</th></tr>
    {% for m in measures %}<tr>
      <td>{{ m['name'] }}</td><td class=muted>{{ m['standard'] }}</td>
      <td class=muted>{{ m['interval_months'] }} mo</td>
      <td class=muted>{{ m['last_inspected'] or '—' }}</td>
      <td class=muted>{{ m['due'] or '—' }}
        {% if m['days'] is not none and m['status']!='compliant' %}
          <div style="font-size:12px">
          {% if m['days']<0 %}{{ -m['days'] }}d overdue{% else %}in {{ m['days'] }}d{% endif %}</div>{% endif %}</td>
      <td><span class="pill {{ m['status'] }}">{{ m['status'].replace('_',' ') }}</span></td>
      <td>
        <a class="btn sm" href="{{ url_for('log_inspection', mid=m['id']) }}">Log inspection</a>
        <a class="btn sm ghost" href="{{ url_for('measure_history', mid=m['id']) }}">History</a>
        <a class="btn sm danger" href="{{ url_for('delete_measure', mid=m['id']) }}"
           onclick="return confirm('Delete this measure?')">✕</a>
      </td>
    </tr>{% endfor %}</table></div>
    <a class="btn ghost danger" href="{{ url_for('delete_building', bid=b['id']) }}"
       onclick="return confirm('Delete this building and all its data?')">Delete building</a>
    """
    return page(body, b=b, measures=measures, score=score, overall=overall)


# ---- Building CRUD --------------------------------------------------------
BUILDING_FORM = """
<div class=card><h2>{{ 'Edit' if b else 'New' }} building</h2>
<form method=post>
  <label>Name</label><input name=name value="{{ b['name'] if b else '' }}" required>
  <label>Address</label><input name=address value="{{ b['address'] if b else '' }}" required>
  <div class=row>
    <div><label>NCC classification</label>
      <input name=classification value="{{ b['classification'] if b else '' }}"
        placeholder="e.g. Class 5 — Office"></div>
    <div><label>Owner / strata contact</label>
      <input name=owner_contact value="{{ b['owner_contact'] if b else '' }}"></div>
  </div>
  <p style="margin-top:16px"><button class=btn>Save</button>
     {% if not b %}<span class=muted style="margin-left:10px">The standard ESM catalogue
     will be added automatically.</span>{% endif %}</p>
</form></div>
"""


@app.route("/building/new", methods=["GET", "POST"])
def add_building():
    if request.method == "POST":
        db = get_db()
        cur = db.execute(
            "INSERT INTO building (name,address,classification,owner_contact,created_at)"
            " VALUES (?,?,?,?,?)",
            (request.form["name"], request.form["address"],
             request.form.get("classification"), request.form.get("owner_contact"),
             datetime.now().isoformat(timespec="seconds")))
        bid = cur.lastrowid
        # auto-attach the standard catalogue, never inspected yet
        for (mname, interval, std) in ESM_CATALOGUE:
            db.execute("INSERT INTO measure "
                       "(building_id,name,standard,interval_months,last_inspected)"
                       " VALUES (?,?,?,?,NULL)", (bid, mname, std, interval))
        db.commit()
        flash("Building created with standard ESM schedule.")
        return redirect(url_for("view_building", bid=bid))
    return page(BUILDING_FORM, b=None)


@app.route("/building/<int:bid>/edit", methods=["GET", "POST"])
def edit_building(bid):
    db = get_db()
    b = db.execute("SELECT * FROM building WHERE id=?", (bid,)).fetchone()
    if not b:
        flash("Building not found.")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        db.execute("UPDATE building SET name=?,address=?,classification=?,owner_contact=? "
                   "WHERE id=?",
                   (request.form["name"], request.form["address"],
                    request.form.get("classification"),
                    request.form.get("owner_contact"), bid))
        db.commit()
        flash("Building updated.")
        return redirect(url_for("view_building", bid=bid))
    return page(BUILDING_FORM, b=b)


@app.route("/building/<int:bid>/delete")
def delete_building(bid):
    db = get_db()
    db.execute("DELETE FROM building WHERE id=?", (bid,))
    db.commit()
    flash("Building deleted.")
    return redirect(url_for("dashboard"))


# ---- Measure CRUD ---------------------------------------------------------
@app.route("/building/<int:bid>/measure/new", methods=["GET", "POST"])
def add_measure(bid):
    db = get_db()
    if request.method == "POST":
        db.execute("INSERT INTO measure "
                   "(building_id,name,standard,interval_months,last_inspected)"
                   " VALUES (?,?,?,?,?)",
                   (bid, request.form["name"], request.form.get("standard"),
                    int(request.form["interval_months"]),
                    request.form.get("last_inspected") or None))
        db.commit()
        flash("Measure added.")
        return redirect(url_for("view_building", bid=bid))
    body = """
    <div class=card><h2>Add safety measure</h2>
    <form method=post>
      <label>Measure name</label>
      <input name=name list=cat required placeholder="e.g. Fire hose reels">
      <datalist id=cat>
        {% for n,i,s in cat %}<option value="{{ n }}">{% endfor %}</datalist>
      <div class=row>
        <div><label>Standard</label><input name=standard placeholder="AS 1851"></div>
        <div><label>Inspection interval (months)</label>
          <input name=interval_months type=number value=12 min=1 required></div>
        <div><label>Last inspected (optional)</label>
          <input name=last_inspected type=date></div>
      </div>
      <p style="margin-top:14px"><button class=btn>Add measure</button></p>
    </form></div>
    """
    return page(body, cat=ESM_CATALOGUE)


@app.route("/measure/<int:mid>/delete")
def delete_measure(mid):
    db = get_db()
    m = db.execute("SELECT building_id FROM measure WHERE id=?", (mid,)).fetchone()
    db.execute("DELETE FROM measure WHERE id=?", (mid,))
    db.commit()
    flash("Measure deleted.")
    return redirect(url_for("view_building", bid=m["building_id"]) if m else url_for("dashboard"))


# ---- Inspections ----------------------------------------------------------
@app.route("/measure/<int:mid>/inspect", methods=["GET", "POST"])
def log_inspection(mid):
    db = get_db()
    m = db.execute("SELECT * FROM measure WHERE id=?", (mid,)).fetchone()
    if not m:
        flash("Measure not found.")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        when = request.form["inspected_on"]
        db.execute("INSERT INTO inspection "
                   "(measure_id,inspected_on,inspector,result,notes)"
                   " VALUES (?,?,?,?,?)",
                   (mid, when, request.form.get("inspector"),
                    request.form["result"], request.form.get("notes")))
        # logging an inspection updates the measure's last_inspected date
        db.execute("UPDATE measure SET last_inspected=? WHERE id=?", (when, mid))
        db.commit()
        flash("Inspection logged and compliance recalculated.")
        return redirect(url_for("view_building", bid=m["building_id"]))
    # If AI analysis was run, prefill the form from the extracted result
    ai = None
    prefill = {"result": "Pass", "notes": ""}
    if request.args.get("report"):
        ai = ai_extract.extract_from_report(request.args["report"])
        prefill["result"] = ai["result"]
        prefill["notes"] = request.args["report"]
    body = """
    <div class=card><h2>🤖 AI report analysis</h2>
    <p class=muted>Paste a free-text inspection report. A local ML model extracts the
       result, severity and a summary — then pre-fills the form below.</p>
    <form method=get>
      <textarea name=report rows=4 placeholder="e.g. Tested all fire hose reels. Reel 3 had low water pressure and a perished hose. Recommend replacement.">{{ request.args.get('report','') }}</textarea>
      <p style="margin-top:10px"><button class=btn>Analyse report</button></p>
    </form>
    {% if ai %}
      <div style="margin-top:6px;padding:14px;border-radius:8px;background:#0f141b;border:1px solid var(--line)">
        <div class=row>
          <div><div class=l style="color:var(--mut);font-size:12px">Result</div>
            <span class="pill {{ 'compliant' if ai['result']=='Pass' else 'due_soon' if ai['result']=='Defect noted' else 'overdue' }}">{{ ai['result'] }}</span></div>
          <div><div class=l style="color:var(--mut);font-size:12px">Severity</div>
            <b>{{ ai['severity'] }}</b></div>
          {% if ai.get('confidence') %}<div><div class=l style="color:var(--mut);font-size:12px">Confidence</div>
            <b>{{ ai['confidence'] }}</b></div>{% endif %}
        </div>
        <div style="margin-top:8px;font-size:12px;color:var(--mut)">Engine: {{ ai['model_used'] }}</div>
        <div style="margin-top:6px">↓ Pre-filled into the form below — review before saving.</div>
      </div>
    {% endif %}
    </div>

    <div class=card><h2>Log inspection — {{ m['name'] }}</h2>
    <form method=post>
      <div class=row>
        <div><label>Date inspected</label>
          <input name=inspected_on type=date value="{{ today }}" required></div>
        <div><label>Inspector / accredited person</label>
          <input name=inspector placeholder="Name or company"></div>
      </div>
      <label>Result</label>
      <select name=result>
        {% for opt in ['Pass','Defect noted','Fail'] %}
          <option {{ 'selected' if opt==prefill['result'] else '' }}>{{ opt }}</option>
        {% endfor %}
      </select>
      <label>Notes</label><textarea name=notes rows=3
        placeholder="Observations, defects, follow-up required...">{{ prefill['notes'] }}</textarea>
      <p style="margin-top:14px"><button class=btn>Save inspection</button></p>
    </form></div>
    """
    return page(body, m=m, today=date.today().isoformat(), ai=ai, prefill=prefill)


@app.route("/measure/<int:mid>/history")
def measure_history(mid):
    db = get_db()
    m = db.execute("SELECT * FROM measure WHERE id=?", (mid,)).fetchone()
    if not m:
        flash("Measure not found.")
        return redirect(url_for("dashboard"))
    rows = db.execute(
        "SELECT * FROM inspection WHERE measure_id=? ORDER BY inspected_on DESC",
        (mid,)).fetchall()
    body = """
    <p><a href="{{ url_for('view_building', bid=m['building_id']) }}">← Back to building</a></p>
    <div class=card><h2>Inspection history — {{ m['name'] }}</h2>
    <table><tr><th>Date</th><th>Inspector</th><th>Result</th><th>Notes</th></tr>
    {% for r in rows %}<tr>
      <td>{{ r['inspected_on'] }}</td><td class=muted>{{ r['inspector'] or '—' }}</td>
      <td><span class="pill {{ 'compliant' if r['result']=='Pass' else 'due_soon' if r['result']=='Defect noted' else 'overdue' }}">{{ r['result'] }}</span></td>
      <td class=muted>{{ r['notes'] or '' }}</td></tr>{% endfor %}
    {% if not rows %}<tr><td colspan=4 class=muted>No inspections logged yet.</td></tr>{% endif %}
    </table></div>
    """
    return page(body, m=m, rows=rows)


# ---- Annual statement generator (the "AFSS-style" output) -----------------
@app.route("/building/<int:bid>/statement")
def statement(bid):
    db = get_db()
    b = db.execute("SELECT * FROM building WHERE id=?", (bid,)).fetchone()
    if not b:
        flash("Building not found.")
        return redirect(url_for("dashboard"))
    measures, counts, score, overall = building_summary(bid)
    can_certify = counts["overdue"] == 0 and counts["never"] == 0
    body = """
    <p><a href="{{ url_for('view_building', bid=b['id']) }}">← Back to building</a></p>
    <div class=card>
      <h2>Annual Fire Safety / ESM Statement (draft)</h2>
      <p class=muted>Generated {{ today }} · functional model — not a legal document</p>
      <h3>{{ b['name'] }}</h3>
      <div class=muted>{{ b['address'] }} · {{ b['classification'] }}</div>
      <p style="margin-top:14px">This statement certifies that each essential safety
         measure listed below has been assessed against its required servicing standard
         and inspection interval.</p>
      <table><tr><th>Measure</th><th>Standard</th><th>Last assessed</th><th>Status</th></tr>
      {% for m in measures %}<tr>
        <td>{{ m['name'] }}</td><td class=muted>{{ m['standard'] }}</td>
        <td class=muted>{{ m['last_inspected'] or 'Never' }}</td>
        <td><span class="pill {{ m['status'] }}">{{ m['status'].replace('_',' ') }}</span></td>
      </tr>{% endfor %}</table>
      <div style="margin-top:18px;padding:14px;border-radius:8px;
         background:{{ 'rgba(46,204,113,.12)' if can_certify else 'rgba(231,76,60,.12)' }};
         border:1px solid {{ 'var(--ok)' if can_certify else 'var(--bad)' }}">
        {% if can_certify %}
          ✓ All measures are current. This building is eligible for statement lodgement.
        {% else %}
          ✕ Statement <b>cannot be certified</b>: {{ counts['overdue'] }} measure(s) overdue
          and {{ counts['never'] }} never inspected. Resolve outstanding items before lodgement.
        {% endif %}
      </div>
    </div>
    """
    return page(body, b=b, measures=measures, counts=counts,
                today=date.today().isoformat())


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    seed_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"SafeKeep running →  http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)