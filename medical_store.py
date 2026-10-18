"""
Medical Store Manager - Flask version (port of the WordPress plugin v2.3.3)

Features: medicines & stock (manual or photo/AI), sales with udhaar (credit),
expiry & low-stock alerts, profit reports, backup/restore, CSV export,
multi-store (owner accounts + admin switcher), subscriptions, Master Catalog
(CSV import/export, search, bulk add).

Run:  pip install flask requests   (Pydroid: Menu > Pip)
Then press the yellow Play button. Open http://localhost:5000 in your phone browser.
Login: admin / admin123   (change it in Settings!)
"""
import base64, csv, io, json, os, re, sqlite3, calendar
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import requests
except ImportError:  # photo-AI is optional
    requests = None

DB_PATH = os.environ.get("MSM_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "msm.db"))
app = Flask(__name__)
app.secret_key = os.environ.get("MSM_SECRET", "change-this-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

# ============================== DATABASE ==============================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'owner',
  sub_until TEXT DEFAULT '', suspended INTEGER DEFAULT 0, view_store INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS store_settings(
  user_id INTEGER PRIMARY KEY, store_name TEXT DEFAULT 'My Medical Store',
  currency TEXT DEFAULT 'Rs', expiry_warn_days INTEGER DEFAULT 90, tagline TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS medicines(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, salt TEXT DEFAULT '',
  company TEXT DEFAULT '', strength TEXT DEFAULT '', batch TEXT DEFAULT '',
  expiry TEXT, purchase_rate REAL DEFAULT 0, mrp REAL DEFAULT 0, sell_price REAL DEFAULT 0,
  stock INTEGER DEFAULT 0, reorder INTEGER DEFAULT 10, supplier TEXT DEFAULT '',
  rack TEXT DEFAULT '', photo TEXT DEFAULT '', store_id INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS i_med_store ON medicines(store_id, name);
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sale_date TEXT NOT NULL, medicine_id INTEGER,
  medicine_name TEXT, qty INTEGER, price REAL, cost REAL, customer TEXT DEFAULT '',
  paid INTEGER DEFAULT 1, store_id INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS i_sale_store ON sales(store_id, sale_date);
CREATE TABLE IF NOT EXISTS catalog(
  id INTEGER PRIMARY KEY AUTOINCREMENT, medicine_name TEXT NOT NULL, generic_name TEXT DEFAULT '',
  manufacturer_company TEXT DEFAULT '', market_authorization_holder TEXT DEFAULT '',
  dosage_form TEXT DEFAULT '', strength TEXT DEFAULT '', pack_size TEXT DEFAULT '',
  active_ingredients TEXT DEFAULT '', therapeutic_category TEXT DEFAULT '',
  registration_number TEXT DEFAULT '', barcode TEXT DEFAULT '', medicine_image_url TEXT DEFAULT '',
  status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS inventory(
  id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER, user_id INTEGER, medicine_id INTEGER,
  quantity INTEGER DEFAULT 0, purchase_price REAL DEFAULT 0, sale_price REAL DEFAULT 0,
  batch_number TEXT DEFAULT '', expiry_date TEXT, rack_location TEXT DEFAULT '',
  low_stock_threshold INTEGER DEFAULT 10, status TEXT DEFAULT 'active',
  created_at TEXT, updated_at TEXT, UNIQUE(store_id, medicine_id));
"""
CAT_COLS = ["medicine_name", "generic_name", "manufacturer_company", "market_authorization_holder",
            "dosage_form", "strength", "pack_size", "active_ingredients", "therapeutic_category",
            "registration_number", "barcode", "medicine_image_url"]
CAT_REQUIRED = [c for c in CAT_COLS if c != "market_authorization_holder"]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        c.execute("INSERT INTO users(username,password_hash,role) VALUES('admin',?, 'admin')",
                  (generate_password_hash("admin123"),))
        print(">> Default login created: admin / admin123  (change it!)")
    c.commit(); c.close()


def q(sql, args=(), one=False):
    cur = db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def ex(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur


# ============================== BASICS ==============================
def today():
    return date.today().isoformat()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def global_settings():
    d = {"pay_contact": "", "grace_days": "3", "api_key": "", "ai_model": "claude-sonnet-5-5",
         "gemini_key": "", "gemini_model": "gemini-2.5-flash"}
    for r in q("SELECT key,value FROM settings"):
        d[r["key"]] = r["value"]
    return d


def set_global(**kw):
    for k, v in kw.items():
        ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))


def current_user():
    if "uid" not in session:
        return None
    if not hasattr(g, "_user"):
        g._user = q("SELECT * FROM users WHERE id=?", (session["uid"],), one=True)
    return g._user


def is_admin():
    u = current_user()
    return bool(u and u["role"] == "admin")


def store_id():
    u = current_user()
    if not u:
        return 0
    if u["role"] == "admin" and u["view_store"]:
        if q("SELECT 1 FROM users WHERE id=?", (u["view_store"],), one=True):
            return u["view_store"]
    return u["id"]


def settings():
    sid = store_id()
    r = q("SELECT * FROM store_settings WHERE user_id=?", (sid,), one=True)
    s = {"store_name": "My Medical Store", "currency": "Rs", "expiry_warn_days": 90, "tagline": ""}
    if r:
        for k in s:
            if r[k] not in (None, ""):
                s[k] = r[k]
    return s


def money(n):
    return f"{settings()['currency']} {float(n or 0):,.2f}"


app.jinja_env.globals["money"] = money


def status_of(m):
    s = settings()
    t = today()
    warn = (date.today() + timedelta(days=int(s["expiry_warn_days"]))).isoformat()
    exp = m["expiry"] or ""
    if exp and exp < t:
        return "EXPIRED"
    if int(m["stock"]) <= 0:
        return "OUT OF STOCK"
    if exp and exp <= warn:
        return "Expiring Soon"
    if int(m["stock"]) <= int(m["reorder"]):
        return "Low Stock"
    return "OK"


def search_meds(text="", only_in_stock=False):
    sql, args = "SELECT * FROM medicines WHERE store_id=?", [store_id()]
    if text:
        like = f"%{text}%"
        sql += " AND (name LIKE ? OR salt LIKE ? OR company LIKE ? OR supplier LIKE ?)"
        args += [like] * 4
    if only_in_stock:
        sql += " AND stock>0"
    return q(sql + " ORDER BY name COLLATE NOCASE", args)


def get_med(mid):
    return q("SELECT * FROM medicines WHERE id=? AND store_id=?", (mid, store_id()), one=True)


def do_sale(mid, qty, price, customer, paid):
    m = get_med(mid)
    if not m:
        return False, "Please select a medicine first."
    if qty <= 0:
        return False, "Quantity must be 1 or more."
    if price < 0:
        return False, "Price cannot be negative."
    if status_of(m) == "EXPIRED":
        return False, "This medicine has EXPIRED, it cannot be sold!"
    if qty > m["stock"]:
        return False, f"Not enough stock. Only {m['stock']} left."
    cur = ex("UPDATE medicines SET stock=stock-? WHERE id=? AND stock>=?", (qty, m["id"], qty))
    if cur.rowcount == 0:
        return False, "Not enough stock."
    ex("INSERT INTO sales(sale_date,medicine_id,medicine_name,qty,price,cost,customer,paid,store_id) "
       "VALUES(?,?,?,?,?,?,?,?,?)",
       (today(), m["id"], m["name"], qty, price, m["purchase_rate"], customer, 1 if paid else 0, store_id()))
    return True, f"Sale done: {money(price*qty)} | Profit: {money((price-m['purchase_rate'])*qty)}"


def sales_between(d1, d2):
    return q("SELECT * FROM sales WHERE sale_date BETWEEN ? AND ? AND store_id=? ORDER BY id DESC",
             (d1, d2, store_id()))


def clean_date(d):
    """'' -> '' (no expiry); invalid -> None; else YYYY-MM-DD."""
    d = (d or "").strip()
    if d == "":
        return ""
    try:
        return datetime.strptime(d, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def normalize_expiry(raw):
    """Turn printed expiry text (08/2027, AUG 27, 2027-08 ...) into YYYY-MM-DD (last day of month)."""
    s = (raw or "").strip().upper()
    if not s:
        return ""
    s = re.sub(r"^(EXP(IRY)?\.?|DATE)\s*[:\-]?\s*", "", s).strip()
    y = m = d = 0
    if (p := re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)):
        y, m, d = map(int, p.groups())
    elif (p := re.match(r"^(\d{1,2})[/\-. ](\d{1,2})[/\-. ](\d{4})$", s)):
        d, m, y = map(int, p.groups())
    elif (p := re.match(r"^(\d{4})[/\-. ](\d{1,2})$", s)):
        y, m = map(int, p.groups())
    elif (p := re.match(r"^(\d{1,2})[/\-. ](\d{4})$", s)):
        m, y = map(int, p.groups())
    elif (p := re.match(r"^(\d{1,2})[/\-. ](\d{2})$", s)):
        m, y = int(p.group(1)), 2000 + int(p.group(2))
    elif (p := re.match(r"^([A-Z]{3})[A-Z]*[/\-., ]*(\d{4}|\d{2})$", s)) and p.group(1) in MONTHS:
        m = MONTHS[p.group(1)]
        y = 2000 + int(p.group(2)) if len(p.group(2)) == 2 else int(p.group(2))
    else:
        return ""
    if not (1 <= m <= 12 and 2000 <= y <= 2100):
        return ""
    last = calendar.monthrange(y, m)[1]
    if d < 1 or d > last:
        d = last
    return f"{y:04d}-{m:02d}-{d:02d}"


# ============================== AUTH / SUBSCRIPTION ==============================
def sub_info(uid):
    gs = global_settings()
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    r = {"state": "none", "until": "", "days": None}
    if u and re.match(r"^\d{4}-\d{2}-\d{2}$", u["sub_until"] or ""):
        r["until"] = u["sub_until"]
        r["days"] = (date.fromisoformat(u["sub_until"]) - date.today()).days
    if u and u["suspended"]:
        r["state"] = "suspended"
        return r
    if r["days"] is None:
        return r
    if r["days"] >= 0:
        r["state"] = "soon" if r["days"] <= 7 else "active"
    elif -r["days"] <= max(0, int(gs["grace_days"] or 0)):
        r["state"] = "grace"
    else:
        r["state"] = "expired"
    return r


def sub_blocked():
    u = current_user()
    if not u or u["role"] == "admin":
        return False
    return sub_info(u["id"])["state"] in ("expired", "suspended")


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        if sub_blocked():
            return render_template("wall.html", info=sub_info(current_user()["id"]),
                                   contact=global_settings()["pay_contact"]), 403
        return f(*a, **kw)
    return w


def admin_required(f):
    @wraps(f)
    @login_required
    def w(*a, **kw):
        if not is_admin():
            return "You are not allowed to do this.", 403
        return f(*a, **kw)
    return w


@app.before_request
def csrf_check():
    if request.method == "POST" and request.endpoint != "login":
        tok = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not tok or tok != session.get("csrf"):
            return "Security check failed. Go back and reload the page.", 400


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = base64.urlsafe_b64encode(os.urandom(24)).decode()
    return session["csrf"]


@app.context_processor
def inject():
    u = current_user()
    ctx = {"csrf": csrf_token, "user": u, "is_admin": is_admin(), "today": today()}
    if u:
        ctx["st"] = settings()
        ctx["stores"] = q("SELECT id,username FROM users WHERE role='owner' OR id=?", (u["id"],)) if is_admin() else []
        ctx["sid"] = store_id()
    return ctx


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE username=?", (request.form.get("username", "").strip(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================== PAGES ==============================
@app.route("/")
@login_required
def dashboard():
    sid, t = store_id(), today()
    mstart = t[:8] + "01"
    d7 = (date.today() - timedelta(days=6)).isoformat()
    day = q("SELECT COALESCE(SUM(qty*price),0) s, COALESCE(SUM(qty*(price-cost)),0) p, COUNT(*) c "
            "FROM sales WHERE sale_date=? AND store_id=?", (t, sid), one=True)
    mon = q("SELECT COALESCE(SUM(qty*price),0) s, COALESCE(SUM(qty*(price-cost)),0) p "
            "FROM sales WHERE sale_date BETWEEN ? AND ? AND store_id=?", (mstart, t, sid), one=True)
    meds = search_meds()
    attn = [m for m in meds if status_of(m) != "OK"]
    stock_val = sum(m["stock"] * m["purchase_rate"] for m in meds)
    credit = q("SELECT COALESCE(SUM(qty*price),0) c FROM sales WHERE paid=0 AND store_id=?", (sid,), one=True)["c"]
    by = {r["d"]: r["s"] for r in q("SELECT sale_date d, SUM(qty*price) s FROM sales WHERE sale_date BETWEEN ? AND ? "
                                    "AND store_id=? GROUP BY sale_date", (d7, t, sid))}
    days = [((date.today() - timedelta(days=i)).isoformat(),) for i in range(6, -1, -1)]
    days = [(d[0], by.get(d[0], 0)) for d in days]
    top = q("SELECT medicine_name n, SUM(qty) q, SUM(qty*price) s FROM sales WHERE sale_date BETWEEN ? AND ? "
            "AND store_id=? GROUP BY medicine_name ORDER BY q DESC LIMIT 5", (mstart, t, sid))
    return render_template("dashboard.html", day=day, mon=mon, alerts=len(attn), attn=attn[:5],
                           stock_val=stock_val, credit=credit, days=days,
                           mx=max([v for _, v in days] + [1]), top=top, status_of=status_of,
                           sub=sub_info(current_user()["id"]) if not is_admin() else None)


@app.route("/sale", methods=["GET", "POST"])
@login_required
def sale():
    if request.method == "POST":
        f = request.form
        ok, msg = do_sale(int(f.get("medicine_id") or 0), int(f.get("qty") or 0), float(f.get("price") or 0),
                          f.get("customer", "").strip(), bool(f.get("paid")))
        flash(msg, "success" if ok else "error")
        return redirect(url_for("sale"))
    meds = search_meds(only_in_stock=True)
    recent = sales_between(today(), today())[:10]
    return render_template("sale.html", meds=[m for m in meds if status_of(m) != "EXPIRED"], recent=recent)


@app.route("/medicines")
@login_required
def medicines():
    text = request.args.get("s", "").strip()
    rows = search_meds(text)
    edit = get_med(int(request.args.get("id") or 0)) if request.args.get("view") == "edit" else None
    return render_template("medicines.html", rows=rows, text=text, edit=edit, status_of=status_of)


@app.post("/medicines/save")
@login_required
def save_medicine():
    mid = int(request.form.get("id") or 0)
    if not get_med(mid):
        flash("To add a new medicine, use Add Stock.", "error")
        return redirect(url_for("addstock"))
    f = {k: request.form.get(k, "").strip() for k in
         ["name", "salt", "company", "strength", "batch", "expiry", "purchase_rate", "mrp", "sell_price",
          "reorder", "supplier", "rack"]}
    if not f["name"]:
        flash("Medicine name is required.", "error")
        return redirect(url_for("medicines", view="edit", id=mid))
    exp = clean_date(f["expiry"])
    if exp is None:
        flash("Expiry date must look like 2027-12-31.", "error")
        return redirect(url_for("medicines", view="edit", id=mid))
    num = lambda k: max(0.0, float(f[k] or 0))
    ex("UPDATE medicines SET name=?,salt=?,company=?,strength=?,batch=?,expiry=?,purchase_rate=?,mrp=?,"
       "sell_price=?,reorder=?,supplier=?,rack=? WHERE id=? AND store_id=?",
       (f["name"], f["salt"], f["company"], f["strength"], f["batch"], exp or None, num("purchase_rate"),
        num("mrp"), num("sell_price"), 10 if f["reorder"] == "" else max(0, int(f["reorder"])),
        f["supplier"], f["rack"], mid, store_id()))
    flash("Medicine saved.", "success")
    return redirect(url_for("medicines"))


@app.post("/medicines/delete")
@login_required
def delete_medicine():
    ex("DELETE FROM medicines WHERE id=? AND store_id=?", (int(request.form.get("id") or 0), store_id()))
    flash("Medicine deleted. Old sales history is kept.", "success")
    return redirect(url_for("medicines"))


@app.route("/addstock", methods=["GET", "POST"])
@login_required
def addstock():
    if request.method == "GET":
        return render_template("addstock.html", d=session.pop("draft", {}))
    d = {k: request.form.get(k, "").strip() for k in
         ["name", "salt", "company", "strength", "batch", "expiry", "mrp", "qty", "purchase_rate",
          "sell_price", "reorder", "supplier", "rack"]}
    exp = clean_date(d["expiry"])
    qty = int(d["qty"] or 0)
    err = ""
    try:
        pr, sp = float(d["purchase_rate"]), float(d["sell_price"])
        if pr < 0 or sp < 0:
            raise ValueError
    except ValueError:
        pr = sp = None
    if not d["name"]:
        err = "Medicine name is required."
    elif exp is None:
        err = "Expiry date must look like 2027-12-31."
    elif qty < 1:
        err = "Quantity must be 1 or more."
    elif pr is None:
        err = "Please type the Purchase price and the Selling price yourself."
    if err:
        session["draft"] = d
        flash(err, "error")
        return redirect(url_for("addstock"))
    mrp = max(0.0, float(d["mrp"] or 0))
    sid = store_id()
    old = q("SELECT * FROM medicines WHERE LOWER(name)=LOWER(?) AND LOWER(strength)=LOWER(?) "
            "AND LOWER(batch)=LOWER(?) AND store_id=? LIMIT 1", (d["name"], d["strength"], d["batch"], sid), one=True)
    if old:
        upd = {"stock": old["stock"] + qty, "purchase_rate": pr, "sell_price": sp}
        if mrp > 0: upd["mrp"] = mrp
        if exp: upd["expiry"] = exp
        if d["reorder"] != "": upd["reorder"] = max(0, int(d["reorder"]))
        for k in ("salt", "company", "supplier", "rack"):
            if d[k]: upd[k] = d[k]
        ex(f"UPDATE medicines SET {','.join(k+'=?' for k in upd)} WHERE id=?", (*upd.values(), old["id"]))
        flash(f"{qty} units added to existing batch of {old['name']}. Stock is now {old['stock']+qty}.", "success")
    else:
        ex("INSERT INTO medicines(name,salt,company,strength,batch,expiry,purchase_rate,mrp,sell_price,stock,"
           "reorder,supplier,rack,store_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (d["name"], d["salt"], d["company"], d["strength"], d["batch"], exp or None, pr, mrp, sp, qty,
            10 if d["reorder"] == "" else max(0, int(d["reorder"])), d["supplier"], d["rack"], sid))
        flash(f"New medicine added: {d['name']} ({qty} units).", "success")
    return redirect(url_for("medicines"))


# ---- photo -> fields (Claude or Gemini; optional) ----
def fields_from_text(text):
    empty = dict(name="", salt="", company="", strength="", batch="", expiry="", mrp="")
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return empty, "AI answer could not be understood. Please type the details yourself."
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return empty, "AI answer could not be understood. Please type the details yourself."
    out = {k: str(j.get(k, "") or "").strip() for k in empty}
    out["expiry"] = normalize_expiry(out["expiry"])
    out["mrp"] = re.sub(r"[^0-9.]", "", out["mrp"])
    return out, ""


PROMPT = ("This is a photo of a medicine pack, strip or bottle. Read ONLY what is actually printed and clearly "
          "visible. Reply with ONLY one JSON object (no other text, no markdown) with keys: \"name\" (brand name), "
          "\"salt\" (generic salt/composition), \"company\" (manufacturer or marketer), \"strength\" (strength and "
          "form, e.g. 500mg Tablet), \"batch\" (batch number), \"expiry\" (expiry date exactly as printed), "
          "\"mrp\" (printed MRP as a plain number). Use an empty string for anything not clearly visible. Never guess.")


@app.post("/scan")
@login_required
def scan():
    empty = dict(name="", salt="", company="", strength="", batch="", expiry="", mrp="")
    f = request.files.get("photo")
    if not f:
        return jsonify(ok=False, message="No photo received.", fields=empty)
    gs = global_settings()
    if requests is None or not (gs["api_key"] or gs["gemini_key"]):
        return jsonify(ok=False, fields=empty,
                       message="No AI key set (Settings). Please type the details yourself.")
    img = base64.b64encode(f.read()).decode()
    try:
        if gs["api_key"]:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                              headers={"x-api-key": gs["api_key"], "anthropic-version": "2023-06-01"},
                              json={"model": gs["ai_model"], "max_tokens": 600, "messages": [{
                                  "role": "user", "content": [
                                      {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
                                      {"type": "text", "text": PROMPT}]}]})
            if r.status_code == 401:
                return jsonify(ok=False, fields=empty, message="The AI key is not valid. Check Settings.")
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        else:
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{gs['gemini_model']}:generateContent",
                              params={"key": gs["gemini_key"]}, timeout=60,
                              json={"contents": [{"parts": [{"inline_data": {"mime_type": "image/jpeg", "data": img}},
                                                            {"text": PROMPT}]}]})
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return jsonify(ok=False, fields=empty, message=f"Could not read photo ({e}). Please type the details.")
    fields, warn = fields_from_text(text)
    return jsonify(ok=not warn, fields=fields, message=warn or "Photo read. Please check every field.")


@app.route("/alerts")
@login_required
def alerts():
    items = [m for m in search_meds() if status_of(m) != "OK"]
    return render_template("alerts.html", items=items, status_of=status_of)


@app.route("/reports")
@login_required
def reports():
    period = request.args.get("period", "today")
    t = today()
    ranges = {"today": (t, t), "month": (t[:8] + "01", t), "all": ("2000-01-01", "2999-12-31")}
    period = period if period in ranges else "today"
    rows = sales_between(*ranges[period])
    total = sum(r["qty"] * r["price"] for r in rows)
    profit = sum(r["qty"] * (r["price"] - r["cost"]) for r in rows)
    stock_val = sum(m["stock"] * m["purchase_rate"] for m in search_meds())
    credit = q("SELECT COALESCE(SUM(qty*price),0) c FROM sales WHERE paid=0 AND store_id=?", (store_id(),), one=True)["c"]
    return render_template("reports.html", rows=rows, period=period, total=total, profit=profit,
                           stock_val=stock_val, credit=credit)


@app.post("/sales/<int:sid>/paid")
@login_required
def mark_paid(sid):
    ex("UPDATE sales SET paid=1 WHERE id=? AND store_id=?", (sid, store_id()))
    flash("Marked as paid.", "success")
    return redirect(url_for("reports", period=request.form.get("period", "today")))


@app.post("/sales/<int:sid>/remove")
@login_required
def remove_sale(sid):
    r = q("SELECT * FROM sales WHERE id=? AND store_id=?", (sid, store_id()), one=True)
    if r:
        ex("UPDATE medicines SET stock=stock+? WHERE id=?", (r["qty"], r["medicine_id"]))
        ex("DELETE FROM sales WHERE id=?", (sid,))
    flash("Sale removed and stock added back.", "success")
    return redirect(url_for("reports", period=request.form.get("period", "today")))


# ---- export / backup / restore ----
def csv_response(name, cols, table):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in q(f"SELECT {','.join(cols)} FROM {table} WHERE store_id=?", (store_id(),)):
        w.writerow([r[c] for c in cols])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.route("/export/medicines")
@login_required
def export_meds():
    return csv_response("medicines", ["id", "name", "salt", "company", "strength", "batch", "expiry",
                                      "purchase_rate", "mrp", "sell_price", "stock", "reorder", "supplier",
                                      "rack"], "medicines")


@app.route("/export/sales")
@login_required
def export_sales():
    return csv_response("sales", ["id", "sale_date", "medicine_id", "medicine_name", "qty", "price", "cost",
                                  "customer", "paid"], "sales")


MED_COLS = ["name", "salt", "company", "strength", "batch", "expiry", "purchase_rate", "mrp", "sell_price",
            "stock", "reorder", "supplier", "rack", "photo"]
SALE_COLS = ["sale_date", "medicine_name", "qty", "price", "cost", "customer", "paid"]


@app.route("/backup", methods=["GET", "POST"])
@login_required
def backup():
    if request.method == "POST" and request.form.get("do") == "restore":
        f = request.files.get("backup_file")
        if not request.form.get("confirm_restore"):
            flash("Please tick the confirmation box first.", "error")
            return redirect(url_for("backup"))
        if not f:
            flash("Please choose a backup file.", "error")
            return redirect(url_for("backup"))
        try:
            j = json.load(f)
            assert isinstance(j["medicines"], list) and isinstance(j["sales"], list)
        except Exception:
            flash("This is not a valid backup file.", "error")
            return redirect(url_for("backup"))
        sid, idmap, d = store_id(), {}, db()
        try:
            d.execute("DELETE FROM medicines WHERE store_id=?", (sid,))
            d.execute("DELETE FROM sales WHERE store_id=?", (sid,))
            for row in j["medicines"]:
                cols = [c for c in MED_COLS if c in row]
                if not cols: continue
                cur = d.execute(f"INSERT INTO medicines({','.join(cols)},store_id) VALUES({','.join('?'*len(cols))},?)",
                                [row[c] for c in cols] + [sid])
                if "id" in row: idmap[row["id"]] = cur.lastrowid
            for row in j["sales"]:
                cols = [c for c in SALE_COLS if c in row]
                if not cols: continue
                d.execute(f"INSERT INTO sales({','.join(cols)},medicine_id,store_id) VALUES({','.join('?'*len(cols))},?,?)",
                          [row[c] for c in cols] + [idmap.get(row.get("medicine_id"), 0), sid])
            d.commit()
        except Exception as e:
            d.rollback()
            flash(f"Restore failed: {e}", "error")
            return redirect(url_for("backup"))
        flash(f"Backup restored: {len(j['medicines'])} medicines, {len(j['sales'])} sales.", "success")
        return redirect(url_for("backup"))
    return render_template("backup.html")


@app.route("/backup/download")
@login_required
def backup_download():
    sid = store_id()
    payload = {"plugin": "medical-store-manager", "version": "flask-1.0", "created": now(),
               "medicines": [dict(r) for r in q("SELECT * FROM medicines WHERE store_id=?", (sid,))],
               "sales": [dict(r) for r in q("SELECT * FROM sales WHERE store_id=?", (sid,))]}
    fn = datetime.now().strftime("medical_backup_%Y%m%d_%H%M.json")
    return Response(json.dumps(payload), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename={fn}"})


# ---- settings ----
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        f, sid = request.form, store_id()
        ex("INSERT INTO store_settings(user_id,store_name,currency,expiry_warn_days,tagline) VALUES(?,?,?,?,?) "
           "ON CONFLICT(user_id) DO UPDATE SET store_name=excluded.store_name,currency=excluded.currency,"
           "expiry_warn_days=excluded.expiry_warn_days,tagline=excluded.tagline",
           (sid, f.get("store_name") or "My Medical Store", f.get("currency") or "Rs",
            max(1, int(f.get("expiry_warn_days") or 90)), f.get("tagline", "")[:80]))
        if is_admin():
            old = global_settings()
            key = "" if f.get("remove_api_key") else (f.get("api_key", "").strip() or old["api_key"])
            gk = "" if f.get("remove_gemini_key") else (f.get("gemini_key", "").strip() or old["gemini_key"])
            set_global(api_key=key, gemini_key=gk,
                       ai_model=re.sub(r"[^a-zA-Z0-9._\-]", "", f.get("ai_model", "")) or "claude-sonnet-5-5",
                       gemini_model=re.sub(r"[^a-zA-Z0-9._\-]", "", f.get("gemini_model", "")) or "gemini-2.5-flash")
        npw = f.get("new_password", "")
        if npw:
            if len(npw) < 6:
                flash("Password must be at least 6 characters.", "error")
                return redirect(url_for("settings_page"))
            ex("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(npw), current_user()["id"]))
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))
    gs = global_settings()
    return render_template("settings.html", gs=gs, has_key=bool(gs["api_key"]), has_gkey=bool(gs["gemini_key"]),
                           orphan=q("SELECT COUNT(*) c FROM medicines WHERE store_id=0", one=True)["c"] if is_admin() else 0)


@app.post("/assign_old")
@admin_required
def assign_old():
    sid = int(request.form.get("store") or 0)
    ex("UPDATE medicines SET store_id=? WHERE store_id=0", (sid,))
    ex("UPDATE sales SET store_id=? WHERE store_id=0", (sid,))
    flash("Old data assigned.", "success")
    return redirect(url_for("settings_page"))


@app.post("/switch_store")
@admin_required
def switch_store():
    sid = int(request.form.get("store") or 0)
    u = q("SELECT * FROM users WHERE id=?", (sid,), one=True)
    if u and (sid == current_user()["id"] or u["role"] == "owner"):
        ex("UPDATE users SET view_store=? WHERE id=?", (sid, current_user()["id"]))
        flash("Store switched.", "success")
    return redirect(request.form.get("back") or url_for("dashboard"))


# ---- subscriptions (admin) ----
@app.route("/subs", methods=["GET", "POST"])
@admin_required
def subs():
    if request.method == "POST":
        f, do = request.form, request.form.get("do")
        if do == "settings":
            set_global(pay_contact=f.get("pay_contact", "")[:60],
                       grace_days=max(0, min(30, int(f.get("grace_days") or 3))))
            flash("Saved.", "success")
        elif do == "new_owner":
            try:
                ex("INSERT INTO users(username,password_hash,role) VALUES(?,?, 'owner')",
                   (f["username"].strip(), generate_password_hash(f["password"])))
                flash("Store owner created.", "success")
            except sqlite3.IntegrityError:
                flash("That username already exists.", "error")
        else:
            uid = int(f.get("uid") or 0)
            u = q("SELECT * FROM users WHERE id=? AND role='owner'", (uid,), one=True)
            if not u:
                flash("Store owner not found.", "error")
            elif do == "extend":
                months = max(1, min(36, int(f.get("months") or 1)))
                cur = u["sub_until"] or ""
                base = date.fromisoformat(cur) if re.match(r"^\d{4}-\d{2}-\d{2}$", cur) and cur >= today() else date.today()
                mth = base.month - 1 + months
                y, m = base.year + mth // 12, mth % 12 + 1
                new = date(y, m, min(base.day, calendar.monthrange(y, m)[1])).isoformat()
                ex("UPDATE users SET sub_until=? WHERE id=?", (new, uid))
                flash(f"Extended until {new}.", "success")
            elif do == "date":
                d = clean_date(f.get("until"))
                if not d:
                    flash("Please choose a valid date.", "error")
                else:
                    ex("UPDATE users SET sub_until=? WHERE id=?", (d, uid))
                    flash(f"Date saved: {d}.", "success")
            elif do == "clear":
                ex("UPDATE users SET sub_until='' WHERE id=?", (uid,))
                flash("Time limit removed.", "success")
            elif do == "suspend":
                ex("UPDATE users SET suspended=1-suspended WHERE id=?", (uid,))
                flash("Store paused/resumed.", "success")
        return redirect(url_for("subs"))
    owners = [(o, sub_info(o["id"])) for o in q("SELECT * FROM users WHERE role='owner' ORDER BY username")]
    return render_template("subs.html", owners=owners, gs=global_settings())


# ============================== MASTER CATALOG ==============================
def cat_clean(src):
    return {k: (src.get(k) or "").strip() for k in CAT_COLS} | {"status": "active"}


@app.route("/catalog/admin", methods=["GET", "POST"])
@admin_required
def catalog_admin():
    if request.method == "POST":
        d = cat_clean(request.form)
        cid = int(request.form.get("id") or 0)
        if d["medicine_name"]:
            d["updated_at"] = now()
            if cid:
                ex(f"UPDATE catalog SET {','.join(k+'=?' for k in d)} WHERE id=?", (*d.values(), cid))
            else:
                d["created_at"] = now()
                ex(f"INSERT INTO catalog({','.join(d)}) VALUES({','.join('?'*len(d))})", tuple(d.values()))
            flash("Medicine saved.", "success")
        return redirect(url_for("catalog_admin"))
    text = request.args.get("s", "").strip()
    like = f"%{text}%"
    rows = q("SELECT * FROM catalog WHERE (?='' OR medicine_name LIKE ? OR generic_name LIKE ? OR "
             "manufacturer_company LIKE ? OR registration_number LIKE ?) ORDER BY medicine_name COLLATE NOCASE LIMIT 100",
             (text, like, like, like, like))
    return render_template("catalog_admin.html", rows=rows, text=text, cols=CAT_COLS, summary=session.pop("imp", None))


@app.post("/catalog/deactivate/<int:cid>")
@admin_required
def catalog_deactivate(cid):
    ex("UPDATE catalog SET status='inactive', updated_at=? WHERE id=?", (now(), cid))
    flash("Medicine deactivated.", "success")
    return redirect(url_for("catalog_admin"))


@app.post("/catalog/import")
@admin_required
def catalog_import():
    f = request.files.get("catalog_csv")
    if not f:
        flash("Please upload a CSV file.", "error")
        return redirect(url_for("catalog_admin"))
    rd = csv.reader(io.StringIO(f.read().decode("utf-8-sig", "replace")))
    headers = next(rd, None)
    if not headers:
        flash("CSV is empty.", "error")
        return redirect(url_for("catalog_admin"))
    headers = [h.strip().lower() for h in headers]
    missing = [c for c in CAT_REQUIRED if c not in headers]
    if missing:
        flash("CSV headers missing. Required: " + ", ".join(CAT_REQUIRED), "error")
        return redirect(url_for("catalog_admin"))
    mode = request.form.get("duplicate_mode", "skip")
    idx = {h: i for i, h in enumerate(headers)}
    total = ok = dups = bad = 0
    errors = []
    for line in rd:
        total += 1
        if len(line) < len(headers):
            bad += 1; errors.append(total + 1); continue
        d = cat_clean({k: line[idx[k]] for k in CAT_REQUIRED})
        if not d["medicine_name"]:
            bad += 1; errors.append(total + 1); continue
        hit = q("SELECT id FROM catalog WHERE LOWER(medicine_name)=LOWER(?) AND LOWER(strength)=LOWER(?) AND "
                "LOWER(pack_size)=LOWER(?) AND LOWER(manufacturer_company)=LOWER(?) LIMIT 1",
                (d["medicine_name"], d["strength"], d["pack_size"], d["manufacturer_company"]), one=True)
        d["updated_at"] = now()
        if hit:
            dups += 1
            if mode == "update":
                ex(f"UPDATE catalog SET {','.join(k+'=?' for k in d)} WHERE id=?", (*d.values(), hit["id"]))
        else:
            d["created_at"] = now()
            ex(f"INSERT INTO catalog({','.join(d)}) VALUES({','.join('?'*len(d))})", tuple(d.values()))
            ok += 1
    session["imp"] = dict(total=total, ok=ok, dups=dups, bad=bad, errors=errors[:20])
    return redirect(url_for("catalog_admin"))


@app.route("/catalog/export")
@admin_required
def catalog_export():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CAT_COLS)
    for r in q(f"SELECT {','.join(CAT_COLS)} FROM catalog ORDER BY medicine_name"):
        w.writerow(list(r))
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=medicine-master-catalog.csv"})


@app.route("/catalog/template")
@admin_required
def catalog_template():
    return Response("\ufeff" + ",".join(CAT_REQUIRED) + "\n", mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=catalog-template.csv"})


@app.route("/catalog")
@login_required
def catalog():
    return render_template("catalog.html")


@app.route("/catalog/search")
@login_required
def catalog_search():
    text = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page") or 1))
    lim, off = 30, (max(1, int(request.args.get("page") or 1)) - 1) * 30
    cols = "id,medicine_name,generic_name,manufacturer_company,strength,pack_size,dosage_form"
    if not text:
        rows = q(f"SELECT {cols} FROM catalog WHERE status='active' ORDER BY medicine_name COLLATE NOCASE LIMIT ? OFFSET ?", (lim, off))
    else:
        like, con = text + "%", f"%{text}%"
        rows = q(f"SELECT {cols} FROM catalog WHERE status='active' AND (medicine_name LIKE ? OR generic_name LIKE ? "
                 "OR manufacturer_company LIKE ? OR registration_number LIKE ?) ORDER BY CASE WHEN medicine_name LIKE ? "
                 "THEN 0 ELSE 1 END, medicine_name COLLATE NOCASE LIMIT ? OFFSET ?", (con, con, con, con, like, lim, off))
    return jsonify(success=True, data=[dict(r) for r in rows])


@app.post("/catalog/bulk_add")
@login_required
def catalog_bulk_add():
    ids = sorted({int(i) for i in request.form.getlist("ids[]") if i.isdigit()})
    if not ids:
        return jsonify(success=False, message="Select at least one medicine.")
    sid, mode = store_id(), request.form.get("duplicate_mode", "skip")
    added = existing = failed = 0
    for cid in ids:
        m = q("SELECT * FROM catalog WHERE id=? AND status='active'", (cid,), one=True)
        if not m:
            failed += 1; continue
        old = q("SELECT id FROM medicines WHERE store_id=? AND LOWER(name)=LOWER(?) AND LOWER(strength)=LOWER(?) LIMIT 1",
                (sid, m["medicine_name"], m["strength"]), one=True)
        if old:
            existing += 1
            if mode == "update":
                ex("UPDATE medicines SET salt=?,company=? WHERE id=?", (m["generic_name"], m["manufacturer_company"], old["id"]))
            continue
        ex("INSERT INTO medicines(name,salt,company,strength,stock,reorder,store_id) VALUES(?,?,?,?,0,10,?)",
           (m["medicine_name"], m["generic_name"], m["manufacturer_company"], m["strength"], sid))
        ex("INSERT OR IGNORE INTO inventory(store_id,user_id,medicine_id,created_at,updated_at) VALUES(?,?,?,?,?)",
           (sid, current_user()["id"], cid, now(), now()))
        added += 1
    return jsonify(success=True, added=added, already=existing, failed=failed,
                   message=f"{added} medicines added. {existing} already in inventory. {failed} failed. "
                           "Set purchase and sale prices before selling.")


# ============================== HTML TEMPLATES ==============================

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:#f0f2f5;color:#1d2327}
nav{background:#0b6b57;color:#fff;display:flex;flex-wrap:wrap;gap:4px;padding:8px 12px;align-items:center}
nav a{color:#fff;text-decoration:none;padding:6px 10px;border-radius:6px;font-size:14px}nav a:hover{background:#ffffff33}
nav .brand{font-weight:700;margin-right:10px}nav .sp{flex:1}
main{max-width:1100px;margin:16px auto;padding:0 12px}
h1{font-size:22px}table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:14px}th{background:#f6f7f7}
.tw{overflow-x:auto}input,select,textarea{padding:9px;border:1px solid #c3c4c7;border-radius:6px;font-size:15px;max-width:100%}
.btn{display:inline-block;padding:8px 14px;border:1px solid #0b6b57;background:#fff;color:#0b6b57;border-radius:6px;cursor:pointer;font-size:14px;text-decoration:none}
.btn.p{background:#0b6b57;color:#fff}.btn.d{border-color:#d63638;color:#d63638}.btn.s{padding:4px 8px;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:12px 0}
.card{background:#fff;border-radius:8px;padding:12px;border-left:4px solid #888;font-size:13px}.card b{display:block;font-size:20px;margin-top:4px}
.card.green{border-color:#1a9b5b}.card.red{border-color:#d63638}.card.amber{border-color:#dba617}
.msg{padding:10px 14px;border-radius:6px;margin:10px 0}.msg.success{background:#d7f0e2}.msg.error{background:#fbd9da}
.st{padding:2px 8px;border-radius:10px;font-size:12px;color:#fff;background:#1a9b5b;white-space:nowrap}
.st.EXPIRED,.st.OUT{background:#d63638}.st.Expiring,.st.Low{background:#dba617}
.box{background:#fff;border-radius:8px;padding:14px;margin:12px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.grid label{display:block;font-size:13px;color:#50575e}.grid input{width:100%}form.inl{display:inline}
.bar{background:#0b6b57;border-radius:4px 4px 0 0;min-height:2px}
.chart{display:flex;gap:8px;align-items:flex-end;height:120px}.chart div{flex:1;text-align:center;font-size:11px}
.cat{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.cc{border:1px solid #ccd0d4;border-radius:8px;background:#fff;padding:12px;font-size:13px}
"""

BASE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}Medical Store{% endblock %}</title><style>""" + CSS + """</style></head><body>
{% if user %}<nav><span class="brand">⚕ {{ st.store_name }}</span>
<a href="/">Dashboard</a><a href="/sale">Sale</a><a href="/medicines">Medicines</a><a href="/addstock">Add Stock</a>
<a href="/catalog">Catalog</a><a href="/alerts">Alerts</a><a href="/reports">Reports</a><a href="/backup">Backup</a><a href="/settings">Settings</a>
{% if is_admin %}<a href="/catalog/admin">Master Catalog</a><a href="/subs">Subscriptions</a>{% endif %}
<span class="sp"></span>
{% if is_admin %}<form class="inl" method="post" action="/switch_store"><input type="hidden" name="_csrf" value="{{ csrf() }}">
<input type="hidden" name="back" value="{{ request.path }}"><select name="store" onchange="this.form.submit()">
{% for s in stores %}<option value="{{ s.id }}" {{ 'selected' if s.id==sid }}>Store: {{ s.username }}</option>{% endfor %}</select></form>{% endif %}
<a href="/logout">Logout ({{ user.username }})</a></nav>{% endif %}
<main>{% with m = get_flashed_messages(with_categories=true) %}{% for c,t in m %}<div class="msg {{ c }}">{{ t }}</div>{% endfor %}{% endwith %}
{% block body %}{% endblock %}</main></body></html>"""

CSRF = '<input type="hidden" name="_csrf" value="{{ csrf() }}">'

TEMPLATES = {
"base.html": BASE,

"login.html": """{% extends 'base.html' %}{% block body %}
<div class="box" style="max-width:340px;margin:60px auto"><h1>Medical Store Login</h1>
<form method="post"><p><input name="username" placeholder="Username" required style="width:100%"></p>
<p><input type="password" name="password" placeholder="Password" required style="width:100%"></p>
<button class="btn p">Login</button></form></div>{% endblock %}""",

"wall.html": """{% extends 'base.html' %}{% block body %}
<div class="box"><h1>{{ 'Store paused' if info.state=='suspended' else 'Subscription ended' }}</h1>
<p>Your access has ended{% if info.until %} (valid until {{ info.until }}){% endif %}. Please renew to continue.</p>
{% if contact %}<p>Contact: <b>{{ contact }}</b></p>{% endif %}<a class="btn" href="/logout">Logout</a></div>{% endblock %}""",

"dashboard.html": """{% extends 'base.html' %}{% block body %}
<h1>Dashboard</h1>
{% if sub and sub.state in ('soon','grace') %}<div class="msg error">Subscription {{ 'ends in '~sub.days~' days' if sub.state=='soon' else 'has ended - grace period' }} ({{ sub.until }}).</div>{% endif %}
<div class="cards"><div class="card green">Today's Sale<b>{{ money(day.s) }}</b>{{ day.c }} sales</div>
<div class="card green">Today's Profit<b>{{ money(day.p) }}</b></div>
<div class="card">This Month Sale<b>{{ money(mon.s) }}</b></div><div class="card">This Month Profit<b>{{ money(mon.p) }}</b></div>
<div class="card amber">Alerts<b>{{ alerts }}</b></div><div class="card">Stock Value<b>{{ money(stock_val) }}</b></div>
<div class="card red">Credit Due<b>{{ money(credit) }}</b></div></div>
<div class="box"><b>Last 7 days</b><div class="chart">{% for d,v in days %}<div><div class="bar" style="height:{{ (v/mx*90)|round }}px"></div>{{ d[5:] }}</div>{% endfor %}</div></div>
<div class="box"><b>Needs attention</b><div class="tw"><table>{% for m in attn %}<tr><td>{{ m.name }} {{ m.strength }}</td><td>{{ m.stock }}</td><td>{{ m.expiry or '-' }}</td><td>{{ status_of(m) }}</td></tr>{% else %}<tr><td>All fine.</td></tr>{% endfor %}</table></div></div>
<div class="box"><b>Top sellers this month</b><div class="tw"><table>{% for t in top %}<tr><td>{{ t.n }}</td><td>{{ t.q }} pcs</td><td>{{ money(t.s) }}</td></tr>{% else %}<tr><td>No sales yet.</td></tr>{% endfor %}</table></div></div>
{% endblock %}""",

"sale.html": """{% extends 'base.html' %}{% block body %}<h1>New Sale</h1>
<form method="post" class="box">""" + CSRF + """<div class="grid">
<div><label>Medicine</label><select name="medicine_id" id="med" required>
{% for m in meds %}<option value="{{ m.id }}" data-p="{{ m.sell_price }}">{{ m.name }} {{ m.strength }} (stock {{ m.stock }})</option>{% endfor %}</select></div>
<div><label>Quantity</label><input type="number" name="qty" id="qty" min="1" value="1" required></div>
<div><label>Price (each)</label><input type="number" step="0.01" name="price" id="price" required></div>
<div><label>Customer</label><input name="customer"></div></div>
<p><label><input type="checkbox" name="paid" checked> Paid (untick for udhaar/credit)</label></p>
<p>Total: <b id="tot">0.00</b></p><button class="btn p">Sell</button></form>
<script>const med=document.getElementById('med'),qty=document.getElementById('qty'),price=document.getElementById('price'),tot=document.getElementById('tot');
function fill(){const o=med.options[med.selectedIndex];if(o)price.value=o.dataset.p;calc()}
function calc(){tot.textContent=((+qty.value||0)*(+price.value||0)).toFixed(2)}
med.onchange=fill;qty.oninput=price.oninput=calc;fill();</script>
<h2>Today's sales</h2><div class="tw"><table><tr><th>Medicine</th><th>Customer</th><th>Qty</th><th>Total</th><th>Paid</th></tr>
{% for r in recent %}<tr><td>{{ r.medicine_name }}</td><td>{{ r.customer }}</td><td>{{ r.qty }}</td><td>{{ money(r.qty*r.price) }}</td><td>{{ 'Yes' if r.paid else 'UDHAAR' }}</td></tr>{% endfor %}</table></div>{% endblock %}""",

"medicines.html": """{% extends 'base.html' %}{% block body %}<h1>Medicines</h1>
<form class="box"><input name="s" value="{{ text }}" placeholder="Search name, salt, company, supplier"> <button class="btn">Search</button>
<a class="btn p" href="/addstock">+ Add Stock</a> <a class="btn" href="/export/medicines">Export CSV</a></form>
{% if edit %}<form method="post" action="/medicines/save" class="box">""" + CSRF + """<input type="hidden" name="id" value="{{ edit.id }}"><h2>Edit {{ edit.name }}</h2><div class="grid">
{% for k,l in [('name','Name'),('salt','Salt'),('company','Company'),('strength','Strength'),('batch','Batch'),('expiry','Expiry (YYYY-MM-DD)'),('purchase_rate','Purchase rate'),('mrp','MRP'),('sell_price','Selling price'),('reorder','Reorder level'),('supplier','Supplier'),('rack','Rack')] %}
<div><label>{{ l }}</label><input name="{{ k }}" value="{{ edit[k] if edit[k] is not none else '' }}"></div>{% endfor %}</div><p><button class="btn p">Save</button> <a class="btn" href="/medicines">Cancel</a></p></form>{% endif %}
<div class="tw"><table><tr><th>Status</th><th>Name</th><th>Company</th><th>Batch</th><th>Expiry</th><th>Stock</th><th>Buy</th><th>Sell</th><th></th></tr>
{% for m in rows %}{% set s=status_of(m) %}<tr><td><span class="st {{ s.split()[0] }}">{{ s }}</span></td><td>{{ m.name }} {{ m.strength }}</td><td>{{ m.company }}</td><td>{{ m.batch }}</td><td>{{ m.expiry or '-' }}</td><td>{{ m.stock }}</td><td>{{ money(m.purchase_rate) }}</td><td>{{ money(m.sell_price) }}</td>
<td><a class="btn s" href="/medicines?view=edit&id={{ m.id }}">Edit</a>
<form class="inl" method="post" action="/medicines/delete" onsubmit="return confirm('Delete?')">""" + CSRF + """<input type="hidden" name="id" value="{{ m.id }}"><button class="btn s d">Delete</button></form></td></tr>
{% else %}<tr><td colspan="9">No medicines yet.</td></tr>{% endfor %}</table></div>{% endblock %}""",

"addstock.html": """{% extends 'base.html' %}{% block body %}<h1>Add Stock</h1>
<div class="box"><b>Photo (optional)</b><p>Take a photo of the pack to auto-fill (needs AI key in Settings), or type manually.</p>
<input type="file" id="photo" accept="image/*" capture="environment"> <span id="scanmsg"></span></div>
<form method="post" class="box">""" + CSRF + """<div class="grid">
{% for k,l in [('name','Medicine name *'),('salt','Salt / generic'),('company','Company'),('strength','Strength'),('batch','Batch'),('expiry','Expiry (YYYY-MM-DD)'),('mrp','MRP'),('qty','Quantity *'),('purchase_rate','Purchase price *'),('sell_price','Selling price *'),('reorder','Reorder level'),('supplier','Supplier'),('rack','Rack')] %}
<div><label>{{ l }}</label><input name="{{ k }}" id="f_{{ k }}" value="{{ d.get(k,'') }}"></div>{% endfor %}</div>
<p><button class="btn p">Save stock</button></p></form>
<script>document.getElementById('photo').onchange=async e=>{const f=e.target.files[0];if(!f)return;
const m=document.getElementById('scanmsg');m.textContent='Reading...';const fd=new FormData();fd.append('photo',f);
const r=await fetch('/scan',{method:'POST',body:fd,headers:{'X-CSRF':'{{ csrf() }}'}});const j=await r.json();
for(const k in j.fields){const el=document.getElementById('f_'+k);if(el&&j.fields[k])el.value=j.fields[k]}m.textContent=j.message};</script>{% endblock %}""",

"alerts.html": """{% extends 'base.html' %}{% block body %}<h1>Alerts (Expiry / Low Stock)</h1>
<p><b>{{ items|length }} medicines need attention</b></p><div class="tw"><table><tr><th>Status</th><th>Medicine</th><th>Stock</th><th>Reorder</th><th>Expiry</th><th>Supplier</th><th></th></tr>
{% for m in items %}{% set s=status_of(m) %}<tr><td><span class="st {{ s.split()[0] }}">{{ s }}</span></td><td>{{ m.name }} {{ m.strength }}</td><td>{{ m.stock }}</td><td>{{ m.reorder }}</td><td>{{ m.expiry or '-' }}</td><td>{{ m.supplier }}</td><td><a class="btn s" href="/addstock">+Stock</a></td></tr>
{% else %}<tr><td colspan="7">Everything is fine.</td></tr>{% endfor %}</table></div>{% endblock %}""",

"reports.html": """{% extends 'base.html' %}{% block body %}<h1>Reports (Sale / Profit)</h1>
<p>{% for k,l in [('today','Today'),('month','This Month'),('all','All')] %}<a class="btn {{ 'p' if k==period }}" href="/reports?period={{ k }}">{{ l }}</a> {% endfor %}
<a class="btn" href="/export/sales">Export sales CSV</a></p>
<div class="cards"><div class="card green">Sale<b>{{ money(total) }}</b></div><div class="card green">Profit<b>{{ money(profit) }}</b></div>
<div class="card">Stock Value<b>{{ money(stock_val) }}</b></div><div class="card red">Credit Due (all time)<b>{{ money(credit) }}</b></div></div>
<div class="tw"><table><tr><th>Date</th><th>Medicine</th><th>Customer</th><th>Qty</th><th>Price</th><th>Total</th><th>Profit</th><th>Paid</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.sale_date }}</td><td>{{ r.medicine_name }}</td><td>{{ r.customer }}</td><td>{{ r.qty }}</td><td>{{ money(r.price) }}</td><td>{{ money(r.qty*r.price) }}</td><td>{{ money(r.qty*(r.price-r.cost)) }}</td>
<td>{{ 'Paid' if r.paid else 'UNPAID' }}</td><td>
{% if not r.paid %}<form class="inl" method="post" action="/sales/{{ r.id }}/paid">""" + CSRF + """<input type="hidden" name="period" value="{{ period }}"><button class="btn s">Mark Paid</button></form>{% endif %}
<form class="inl" method="post" action="/sales/{{ r.id }}/remove" onsubmit="return confirm('Stock will be added back. Remove this sale?')">""" + CSRF + """<input type="hidden" name="period" value="{{ period }}"><button class="btn s d">Remove</button></form></td></tr>
{% else %}<tr><td colspan="9">No sales in this period.</td></tr>{% endfor %}</table></div>{% endblock %}""",

"backup.html": """{% extends 'base.html' %}{% block body %}<h1>Backup / Restore</h1>
<div class="box"><a class="btn p" href="/backup/download">Download backup (JSON)</a> <a class="btn" href="/export/medicines">Medicines CSV</a> <a class="btn" href="/export/sales">Sales CSV</a></div>
<form method="post" enctype="multipart/form-data" class="box">""" + CSRF + """<input type="hidden" name="do" value="restore"><h2>Restore</h2>
<p>This REPLACES this store's medicines and sales with the file's data.</p><input type="file" name="backup_file" accept=".json" required>
<p><label><input type="checkbox" name="confirm_restore"> I understand current data will be replaced</label></p><button class="btn d">Restore</button></form>{% endblock %}""",

"settings.html": """{% extends 'base.html' %}{% block body %}<h1>Settings</h1>
<form method="post" class="box">""" + CSRF + """<div class="grid">
<div><label>Store name</label><input name="store_name" value="{{ st.store_name }}"></div>
<div><label>Currency</label><input name="currency" value="{{ st.currency }}"></div>
<div><label>Expiry warning (days)</label><input type="number" name="expiry_warn_days" value="{{ st.expiry_warn_days }}"></div>
<div><label>Tagline</label><input name="tagline" value="{{ st.tagline }}" maxlength="80"></div>
<div><label>New password (optional)</label><input type="password" name="new_password"></div></div>
{% if is_admin %}<h2>AI photo reader (admin only)</h2><div class="grid">
<div><label>Claude API key {{ '(saved)' if has_key }}</label><input type="password" name="api_key" placeholder="leave blank to keep"></div>
<div><label>Claude model</label><input name="ai_model" value="{{ gs.ai_model }}"></div>
<div><label>Gemini key {{ '(saved)' if has_gkey }}</label><input type="password" name="gemini_key" placeholder="leave blank to keep"></div>
<div><label>Gemini model</label><input name="gemini_model" value="{{ gs.gemini_model }}"></div></div>
<p><label><input type="checkbox" name="remove_api_key"> Remove Claude key</label> <label><input type="checkbox" name="remove_gemini_key"> Remove Gemini key</label></p>{% endif %}
<p><button class="btn p">Save</button></p></form>
{% if is_admin and orphan %}<form method="post" action="/assign_old" class="box">""" + CSRF + """<b>{{ orphan }} old medicines have no store.</b>
<select name="store">{% for s in stores %}<option value="{{ s.id }}">{{ s.username }}</option>{% endfor %}</select> <button class="btn">Assign to store</button></form>{% endif %}{% endblock %}""",

"subs.html": """{% extends 'base.html' %}{% block body %}<h1>Subscriptions</h1>
<form method="post" class="box">""" + CSRF + """<input type="hidden" name="do" value="new_owner"><b>New store owner</b>
<input name="username" placeholder="username" required> <input name="password" placeholder="password" required> <button class="btn p">Create</button></form>
<form method="post" class="box">""" + CSRF + """<input type="hidden" name="do" value="settings"><b>Payment contact (WhatsApp / phone)</b>
<input name="pay_contact" value="{{ gs.pay_contact }}"> Grace days <input type="number" name="grace_days" value="{{ gs.grace_days }}" style="width:70px"> <button class="btn">Save</button></form>
<div class="tw"><table><tr><th>Owner</th><th>State</th><th>Until</th><th>Actions</th></tr>
{% for o,i in owners %}<tr><td>{{ o.username }}</td><td>{{ i.state }}{% if i.days is not none %} ({{ i.days }}d){% endif %}</td><td>{{ i.until or '-' }}</td><td>
<form class="inl" method="post">""" + CSRF + """<input type="hidden" name="uid" value="{{ o.id }}"><input type="hidden" name="do" value="extend"><input type="number" name="months" value="1" min="1" max="36" style="width:60px"><button class="btn s">+Months</button></form>
<form class="inl" method="post">""" + CSRF + """<input type="hidden" name="uid" value="{{ o.id }}"><input type="hidden" name="do" value="date"><input name="until" placeholder="YYYY-MM-DD" style="width:110px"><button class="btn s">Set date</button></form>
<form class="inl" method="post">""" + CSRF + """<input type="hidden" name="uid" value="{{ o.id }}"><input type="hidden" name="do" value="clear"><button class="btn s">No limit</button></form>
<form class="inl" method="post">""" + CSRF + """<input type="hidden" name="uid" value="{{ o.id }}"><input type="hidden" name="do" value="suspend"><button class="btn s d">{{ 'Resume' if o.suspended else 'Pause' }}</button></form></td></tr>
{% else %}<tr><td colspan="4">No store owners yet.</td></tr>{% endfor %}</table></div>{% endblock %}""",

"catalog.html": """{% extends 'base.html' %}{% block body %}<h1>Medicine Catalog &amp; Bulk Add</h1>
<p>Search the catalog, select several medicines and add them to your store. Then set prices from Medicines.</p>
<input id="q" type="search" placeholder="Name, generic, company or registration number..." style="width:min(700px,100%);padding:12px">
<p><button class="btn" id="go">Search</button> <button class="btn" id="all">Select all on page</button> <button class="btn" id="clr">Clear</button>
<select id="mode"><option value="skip">Skip already added</option><option value="update">Update already added</option></select>
<button class="btn p" id="add">Add selected</button> <span id="count">0 selected</span></p><div id="msg"></div><div class="cat" id="res"></div>
<p><button class="btn" id="more">Load more</button></p>
<script>const sel=new Set();let page=1,res=document.getElementById('res');
async function load(reset){if(reset){page=1;res.innerHTML=''}const r=await fetch('/catalog/search?q='+encodeURIComponent(q.value)+'&page='+page);const j=await r.json();
j.data.forEach(m=>{const d=document.createElement('label');d.className='cc';d.innerHTML='<input type="checkbox" '+(sel.has(m.id)?'checked':'')+'> <b></b><br><small></small>';
d.querySelector('b').textContent=m.medicine_name+' '+m.strength;d.querySelector('small').textContent=[m.generic_name,m.manufacturer_company,m.dosage_form,m.pack_size].filter(Boolean).join(' | ');
const c=d.querySelector('input');c.dataset.id=m.id;c.onchange=()=>{c.checked?sel.add(m.id):sel.delete(m.id);upd()};res.appendChild(d)})}
function upd(){count.textContent=sel.size+' selected'}
go.onclick=()=>load(true);more.onclick=()=>{page++;load(false)};q.oninput=()=>{clearTimeout(window.t);window.t=setTimeout(()=>load(true),300)};
all.onclick=()=>{res.querySelectorAll('input').forEach(c=>{c.checked=true;sel.add(+c.dataset.id)});upd()};
clr.onclick=()=>{sel.clear();res.querySelectorAll('input').forEach(c=>c.checked=false);upd()};
add.onclick=async()=>{const fd=new FormData();sel.forEach(i=>fd.append('ids[]',i));fd.append('duplicate_mode',mode.value);
const r=await fetch('/catalog/bulk_add',{method:'POST',body:fd,headers:{'X-CSRF':'{{ csrf() }}'}});const j=await r.json();
msg.innerHTML='<div class="msg '+(j.success?'success':'error')+'"></div>';msg.firstChild.textContent=j.message;if(j.success){sel.clear();upd();load(true)}};
load(true);</script>{% endblock %}""",

"catalog_admin.html": """{% extends 'base.html' %}{% block body %}<h1>Medicine Master Catalog</h1>
{% if summary %}<div class="msg success">Import: total {{ summary.total }}, imported {{ summary.ok }}, duplicates {{ summary.dups }}, invalid {{ summary.bad }}{% if summary.errors %} (bad rows: {{ summary.errors|join(', ') }}){% endif %}</div>{% endif %}
<form method="post" action="/catalog/import" enctype="multipart/form-data" class="box">""" + CSRF + """<b>Import CSV</b>
<input type="file" name="catalog_csv" accept=".csv" required> <select name="duplicate_mode"><option value="skip">Skip duplicates</option><option value="update">Update duplicates</option></select>
<button class="btn p">Import</button> <a class="btn" href="/catalog/template">Template</a> <a class="btn" href="/catalog/export">Export</a></form>
<form method="post" class="box">""" + CSRF + """<input type="hidden" name="id" value="0"><b>Add medicine</b><div class="grid">
{% for c in cols if c!='medicine_image_url' or true %}<div><label>{{ c.replace('_',' ') }}</label><input name="{{ c }}"></div>{% endfor %}</div><p><button class="btn p">Save medicine</button></p></form>
<form class="box"><input name="s" value="{{ text }}" placeholder="Search catalog"> <button class="btn">Search</button></form>
<div class="tw"><table><tr><th>Name</th><th>Generic</th><th>Company</th><th>Strength</th><th>Registration</th><th>Status</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.medicine_name }}</td><td>{{ r.generic_name }}</td><td>{{ r.manufacturer_company }}</td><td>{{ r.strength }}</td><td>{{ r.registration_number }}</td><td>{{ r.status }}</td>
<td>{% if r.status=='active' %}<form class="inl" method="post" action="/catalog/deactivate/{{ r.id }}">""" + CSRF + """<button class="btn s d">Deactivate</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="7">No records.</td></tr>{% endfor %}</table></div>{% endblock %}""",
}

app.jinja_loader = DictLoader(TEMPLATES)

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
