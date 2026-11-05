# =====================================================================
#  BYTE HOST — multi-user Python bot hosting (single file)
#  python-telegram-bot v22.x   |   env: BOT_TOKEN, OWNER_ID
#  optional env: MAX_PY_KB (512), MEM_LIMIT_MB (0=off), REG_MSG_ID, PORT
# =====================================================================
import asyncio, html, json, os, re, shutil, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

HTML = ParseMode.HTML
TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
MAX_KB = int(os.environ.get("MAX_PY_KB", "512"))
MEM_MB = int(os.environ.get("MEM_LIMIT_MB", "0"))
BASE = os.path.abspath("hosted")
os.makedirs(BASE, exist_ok=True)
T0 = time.time()

# ---------------------------------------------------------------------
#  PREMIUM EMOJI (id, fallback)
# ---------------------------------------------------------------------
EM = {
    "crown": ("5217822164362739968", "👑"), "bot": ("5282843764451195532", "🖥"),
    "user": ("5461117441612462242", "🙂"), "ok": ("5206607081334906820", "✔️"),
    "no": ("5210952531676504517", "❌"), "warn": ("5447644880824181073", "⚠️"),
    "on": ("5416081784641168838", "🟢"), "off": ("5411225014148014586", "🔴"),
    "play": ("5264919878082509254", "▶️"), "stop": ("5260293700088511294", "⛔️"),
    "re": ("5375338737028841420", "🔄"), "log": ("5443038326535759644", "💬"),
    "stat": ("5231200819986047254", "📊"), "file": ("5305265301917549162", "📎"),
    "edit": ("5395444784611480792", "✏️"), "dl": ("5406745015365943482", "⬇️"),
    "pkg": ("5406683434124859552", "🛍"), "lock": ("5296369303661067030", "🔒"),
    "gear": ("5341715473882955310", "⚙️"), "trash": ("5445267414562389170", "🗑"),
    "back": ("5449683594425410231", "🔼"), "home": ("5416041192905265756", "🏠"),
    "plus": ("5397916757333654639", "➕"), "bulb": ("5422439311196834318", "💡"),
    "mega": ("5424818078833715060", "📣"), "ban": ("5240241223632954241", "🚫"),
    "shield": ("5251203410396458957", "🛡"), "clock": ("5386367538735104399", "⌛"),
    "cal": ("5413879192267805083", "🗓"), "party": ("5461151367559141950", "🎉"),
    "siren": ("5395695537687123235", "🚨"), "info": ("5334544901428229844", "ℹ️"),
    "zap": ("5456140674028019486", "⚡️"), "arrow": ("5416117059207572332", "➡️"),
    "star": ("5438496463044752972", "⭐️"), "gem": ("5427168083074628963", "💎"),
    "pin": ("5397782960512444700", "📌"), "bell": ("5458603043203327669", "🔔"),
    "mail": ("5253742260054409879", "✉️"), "hundred": ("5341498088408234504", "💯"),
    "link": ("5271604874419647061", "🔗"), "new": ("5382357040008021292", "🆕"),
}
PREM = {"btn": True}


def E(k):
    i, f = EM[k]
    return f'<tg-emoji emoji-id="{i}">{f}</tg-emoji>'


def esc(s):
    return html.escape(str(s), quote=False)


def bf(s):  # unicode bold sans (button labels can't use <b>)
    o = []
    for ch in s:
        x = ord(ch)
        if 65 <= x <= 90: o.append(chr(0x1D5D4 + x - 65))
        elif 97 <= x <= 122: o.append(chr(0x1D5EE + x - 97))
        elif 48 <= x <= 57: o.append(chr(0x1D7EC + x - 48))
        else: o.append(ch)
    return "".join(o)


def B(label, cb, ico=None, st=None):
    return (label, cb, ico, st)


def mk(rows):
    out = []
    for row in rows:
        r = []
        for label, cb, ico, st in row:
            kw, text = {}, bf(label)
            if PREM["btn"]:
                if ico: kw["icon_custom_emoji_id"] = EM[ico][0]
                if st: kw["style"] = st
            elif ico:
                text = EM[ico][1] + " " + text
            try:
                r.append(InlineKeyboardButton(text, callback_data=cb, **kw))
            except TypeError:  # older PTB without native fields
                r.append(InlineKeyboardButton(text, callback_data=cb, api_kwargs=kw))
        out.append(r)
    return InlineKeyboardMarkup(out)


async def tg(call, rows=None, **kw):
    """send/edit with automatic fallback if premium button icons are rejected"""
    kb = mk(rows) if rows else None
    try:
        return await call(parse_mode=HTML, reply_markup=kb, **kw)
    except BadRequest as e:
        s = str(e).lower()
        if "not modified" in s: return None
        if PREM["btn"] and rows and any(w in s for w in ("icon", "emoji", "style", "button")):
            PREM["btn"] = False
            return await tg(call, rows, **kw)
        raise


# ---------------------------------------------------------------------
#  STATE
# ---------------------------------------------------------------------
reg = {}
reg_msg_id = None
procs, meta, crashlog, busy = {}, {}, {}, set()
save_lock = None
BOOTED = None
ERR = re.compile(r"Traceback \(most recent|InvalidToken|Unauthorized|Conflict:|"
                 r"ModuleNotFoundError|ImportError|SyntaxError", re.I)
RISK = ("/proc", "subprocess", "os.system", "rmtree", "hosted", "exec(", "eval(", "base64", "OWNER_ID")


def norm():
    now = int(time.time())
    reg.setdefault("next", 1); reg.setdefault("bots", {}); reg.setdefault("users", {})
    cfg = reg.setdefault("cfg", {})
    cfg.setdefault("maint", False); cfg.setdefault("approval", False); cfg.setdefault("limit", 1)
    for b in reg["bots"].values():
        for k, v in (("uid", OWNER), ("env", {}), ("auto", True), ("every", 0), ("notify", True),
                     ("created", now), ("starts", 0), ("crashes", 0), ("req", ""), ("run", False),
                     ("approved", True), ("name", "bot.py")):
            b.setdefault(k, v)
    reg["users"].setdefault(str(OWNER), {"name": "Owner", "un": "", "joined": now})


norm()


def ubots(uid): return [i for i, b in reg["bots"].items() if b["uid"] == uid]


def ulimit(uid):
    if uid == OWNER: return 10 ** 9
    return reg["users"].get(str(uid), {}).get("limit", reg["cfg"]["limit"])


def can(uid, n):
    b = reg["bots"].get(str(n))
    return bool(b and (uid == OWNER or b["uid"] == uid))


def uname(uid):
    return reg["users"].get(str(uid), {}).get("name") or str(uid)


def reqlines(b): return [l for l in b.get("req", "").splitlines() if l.strip()]


# ---------------------------------------------------------------------
#  web ping (Render needs an open port)
# ---------------------------------------------------------------------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"alive")

    def log_message(self, *a): pass


def web():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Ping).serve_forever()


# ---------------------------------------------------------------------
#  small utils
# ---------------------------------------------------------------------
def dur(s):
    s = int(s); d, s = divmod(s, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    p = []
    if d: p.append(f"{d}d")
    if h: p.append(f"{h}h")
    if m: p.append(f"{m}m")
    if s and not d and not h: p.append(f"{s}s")
    return " ".join(p) or "0s"


def dt(ts): return time.strftime("%d %b %Y", time.localtime(ts))
def tail(t, n=1400): return t[-n:]


def sysinfo():
    try:
        mi = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
        tot = mi["MemTotal"] // 1024; av = mi.get("MemAvailable", 0) // 1024
        ram = f"{tot - av}/{tot} MB"
    except Exception:
        ram = "n/a"
    try: ld = f"{os.getloadavg()[0]:.2f}"
    except Exception: ld = "n/a"
    return ram, ld


def diagnose(t):
    m = re.search(r"No module named '([\w.\-]+)'", t)
    if m: return f"Package missing: {m.group(1).split('.')[0]} — Packages mein add karo."
    if re.search(r"InvalidToken|Unauthorized|token.{0,20}(invalid|rejected)", t, re.I):
        return "Bot token galat/expired hai — Env vars mein sahi BOT_TOKEN daalo."
    m = re.search(r"KeyError: '([A-Za-z0-9_]+)'", t)
    if m: return f"Env missing: {m.group(1)} — Env vars mein add karo."
    if "Conflict" in t: return "Same token par dusra instance chal raha hai (409) — ek hi jagah chalao."
    if "SyntaxError" in t: return "Code mein syntax error hai — file fix karke update karo."
    return "Logs dekho aur file / packages / env check karo."


def check_py(data):
    try: src = data.decode("utf-8")
    except UnicodeDecodeError: return "File UTF-8 nahi hai"
    try: compile(src, "main.py", "exec")
    except SyntaxError as e: return f"{e.msg} (line {e.lineno})"
    except Exception as e: return str(e)[:100]
    return None


def parse_env(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if line.startswith("export "): line = line[7:].strip()
        k, sep, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k): continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        out[k] = v
    return out


REQ_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[\w,\-]+\])?"
                    r"([<>=!~]=?[\w.*+!\-]+(,[<>=!~]=?[\w.*+!\-]+)*)?$")


def clean_req(raw, is_owner):
    lines, bad = [], []
    for ln in raw.splitlines():
        ln = ln.split("#")[0].strip()
        if not ln: continue
        ln2 = re.sub(r"\s+", "", ln)
        if REQ_OK.match(ln2): lines.append(ln2)
        elif is_owner: lines.append(ln)
        else: bad.append(ln)
    return lines, bad


def req_name(l): return re.split(r"[<>=!~\[;@ ]", l)[0].lower()


# ---------------------------------------------------------------------
#  process management
# ---------------------------------------------------------------------
def bdir(n): return f"{BASE}/bot{n}"
def pyp(n): return f"{bdir(n)}/main.py"
def logp(n): return f"{bdir(n)}/bot.log"
def reqp(n): return f"{bdir(n)}/requirements.txt"


def alive(n):
    p = procs.get(n)
    return bool(p and p.poll() is None)


def rss(n):
    p = procs.get(n)
    try:
        for l in open(f"/proc/{p.pid}/status"):
            if l.startswith("VmRSS"): return int(l.split()[1]) // 1024
    except Exception: pass
    return None


def stop(n):
    p = procs.pop(n, None)
    if p and p.poll() is None:
        try: os.killpg(p.pid, signal.SIGTERM)
        except Exception: p.terminate()
        try: p.wait(5)
        except Exception:
            try: os.killpg(p.pid, signal.SIGKILL)
            except Exception: p.kill()


def child_env(n):
    # host secrets never reach hosted bots; only the bot's own env is injected
    env = {k: v for k, v in os.environ.items() if k not in ("BOT_TOKEN", "OWNER_ID", "PORT", "REG_MSG_ID")}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(reg["bots"].get(str(n), {}).get("env", {}))
    return env


def run(n):
    stop(n)
    os.makedirs(bdir(n), exist_ok=True)
    lp = logp(n)
    if os.path.exists(lp) and os.path.getsize(lp) > 300_000: os.remove(lp)
    with open(lp, "ab") as f:
        f.write(f"\n--- start {time.strftime('%d-%b %H:%M:%S')} ---\n".encode())
    off = os.path.getsize(lp)
    kw = {}
    if MEM_MB and hasattr(os, "fork"):
        import resource
        kw["preexec_fn"] = lambda: resource.setrlimit(resource.RLIMIT_AS, (MEM_MB << 20, MEM_MB << 20))
    out = open(lp, "ab")
    procs[n] = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=bdir(n), stdout=out,
                                stderr=subprocess.STDOUT, env=child_env(n),
                                start_new_session=True, **kw)
    out.close()
    meta[n] = {"t0": time.time(), "off": off}
    b = reg["bots"][str(n)]
    b["starts"] = b.get("starts", 0) + 1
    b["last_start"] = int(time.time())


def newlog(n):
    try:
        with open(logp(n), "rb") as f:
            f.seek(meta.get(n, {}).get("off", 0))
            return f.read().decode(errors="ignore")
    except Exception:
        return ""


def fulllog(n):
    try: return open(logp(n), errors="ignore").read()
    except Exception: return ""


def pip_install(n, text):
    os.makedirs(bdir(n), exist_ok=True)
    open(reqp(n), "w").write(text)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-input",
                            "--disable-pip-version-check", "-r", reqp(n)],
                           capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        return False, "timeout (7 min)"
    return r.returncode == 0, (r.stdout + r.stderr)[-900:]


def flags(n):
    try: src = open(pyp(n), errors="ignore").read()
    except Exception: return []
    return [r for r in RISK if r in src]


# ---------------------------------------------------------------------
#  registry persistence (pinned document in owner chat)
# ---------------------------------------------------------------------
async def save(bot):
    async with save_lock:
        await _save(bot, True)


async def _save(bot, retry):
    global reg_msg_id
    data = json.dumps(reg, ensure_ascii=False).encode()
    cap = f"REGISTRY • {len(reg['users'])}u/{len(reg['bots'])}b"
    try:
        if reg_msg_id:
            await bot.edit_message_media(
                chat_id=OWNER, message_id=reg_msg_id,
                media=InputMediaDocument(data, caption=cap, filename="registry.json"))
        else:
            m = await bot.send_document(OWNER, document=data, filename="registry.json", caption=cap)
            await bot.pin_chat_message(OWNER, m.message_id, disable_notification=True)
            reg_msg_id = m.message_id
            await bot.send_message(OWNER, f"Registry message id: {m.message_id}\n"
                                          "(optional backup: Render env REG_MSG_ID me daal do)")
    except BadRequest as e:
        s = str(e).lower()
        if "not modified" in s: return
        if ("not found" in s or "can't be edited" in s) and retry:
            reg_msg_id = None
            return await _save(bot, False)
        print("save error:", e)
    except Exception as e:
        print("save error:", e)


async def load_msg(pm):
    if pm.document and (pm.caption or "").startswith("REGISTRY"):
        f = await pm.document.get_file()
        return json.loads(bytes(await f.download_as_bytearray()))
    if pm.text and pm.text.startswith("REGISTRY\n"):  # old text-registry format
        return json.loads(pm.text.split("\n", 1)[1])
    return None


# ---------------------------------------------------------------------
#  messaging helpers
# ---------------------------------------------------------------------
async def show(u, c, text, rows=None):
    q = u.callback_query
    if q:
        try:
            return await tg(q.edit_message_text, rows, text=text)
        except BadRequest as e:
            s = str(e).lower()
            if "no text" not in s and "can't be edited" not in s: raise
    return await tg(c.bot.send_message, rows, chat_id=u.effective_chat.id, text=text)


async def say(c, chat_id, text, rows=None):
    try:
        return await tg(c.bot.send_message, rows, chat_id=chat_id, text=text)
    except Exception as e:
        print("say error:", e)


async def ping(bot, uid, text, rows=None):
    try: return await tg(bot.send_message, rows, chat_id=uid, text=text)
    except Exception: return None


# ---------------------------------------------------------------------
#  screens
# ---------------------------------------------------------------------
def home_screen(uid):
    mine = ubots(uid)
    lim = "∞" if uid == OWNER else ulimit(uid)
    live = sum(1 for i in mine if alive(int(i)))
    t = (f"{E('crown')} <b>BYTE HOST</b>\n"
         f"<blockquote><b>{E('zap')} Python bot hosting — fast • simple • secure</b></blockquote>\n\n"
         f"{E('user')} <b>User:</b> <code>{esc(uname(uid))}</code>\n"
         f"{E('bot')} <b>Slots:</b> <code>{len(mine)}/{lim}</code>\n"
         f"{E('on')} <b>Live:</b> <code>{live}</code>")
    rows = [[B("Host new bot", "new", "plus", "success"), B("My bots", "l:m:0", "bot", "primary")],
            [B("Profile", "me", "user"), B("Guide", "help", "bulb")]]
    if uid == OWNER: rows.append([B("Admin panel", "adm", "crown", "primary")])
    return t, rows


def profile_screen(uid):
    u = reg["users"].get(str(uid), {})
    lim = "∞" if uid == OWNER else ulimit(uid)
    t = (f"{E('user')} <b>PROFILE</b>\n<blockquote>"
         f"<b>{E('pin')} Name:</b> <code>{esc(uname(uid))}</code>\n"
         f"<b>{E('link')} User:</b> <code>@{esc(u.get('un') or '-')}</code>\n"
         f"<b>{E('star')} ID:</b> <code>{uid}</code>\n"
         f"<b>{E('cal')} Joined:</b> <code>{dt(u.get('joined', time.time()))}</code>\n"
         f"<b>{E('bot')} Bots:</b> <code>{len(ubots(uid))}/{lim}</code>\n"
         f"<b>{E('gem')} Plan:</b> <code>{'OWNER' if uid == OWNER else 'FREE'}</code></blockquote>")
    return t, [[B("Home", "home", "home")]]


def help_screen():
    t = (f"{E('bulb')} <b>GUIDE</b>\n"
         f"<blockquote expandable><b>{E('arrow')} 1.</b> <b>Host new bot</b> dabao aur apni <code>.py</code> file bhejo\n"
         f"<b>{E('arrow')} 2.</b> Packages bhejo (<code>requests pillow</code>) ya Skip karo\n"
         f"<b>{E('arrow')} 3.</b> Env bhejo — <code>BOT_TOKEN=123:ABC</code> (ya <code>.env</code> file)\n"
         f"<b>{E('arrow')} 4.</b> Bot start hote hi <b>Successfully / Failed</b> report milegi\n\n"
         f"<b>{E('lock')} Env:</b> code mein <code>os.environ[\"BOT_TOKEN\"]</code> use karo — "
         f"host ka token tumhare bot ko kabhi nahi milta\n"
         f"<b>{E('re')} Auto-restart:</b> crash par bot khud restart hota hai (Settings se control)\n"
         f"<b>{E('warn')} Note:</b> host restart par disk reset hota hai — bot ki banayi files "
         f"(db/json) delete ho sakti hain; file, packages aur env safe rehte hain\n"
         f"<b>{E('ban')} Limit:</b> ek user = default 1 bot</blockquote>")
    return t, [[B("Home", "home", "home")]]


def panel(n, viewer):
    b = reg["bots"][str(n)]
    a = alive(n)
    m = meta.get(n, {})
    up = dur(time.time() - m["t0"]) if a and m else "—"
    ram = rss(n)
    req = ", ".join(reqlines(b)) or "—"
    pid = procs[n].pid if a else "—"
    status = "RUNNING" if a else ("PENDING APPROVAL" if b.get("pending") else "STOPPED")
    ram_s = f"{ram} MB" if ram else "—"
    t = (f"{E('bot')} <b>bot[{n}]</b> <code>{esc(b['name'])}</code>\n"
         f"<blockquote>"
         f"<b>{E('on' if a else 'off')} Status:</b> <code>{status}</code>\n"
         f"<b>{E('clock')} Uptime:</b> <code>{up}</code>\n"
         f"<b>{E('stat')} RAM:</b> <code>{ram_s}</code>  <b>PID:</b> <code>{pid}</code>\n"
         f"<b>{E('re')} Starts:</b> <code>{b.get('starts', 0)}</code>  "
         f"<b>{E('siren')} Crashes:</b> <code>{b.get('crashes', 0)}</code></blockquote>\n"
         f"<blockquote expandable>"
         f"<b>{E('pkg')} Packages:</b> <code>{esc(req)[:200]}</code>\n"
         f"<b>{E('lock')} Env vars:</b> <code>{len(b.get('env', {}))}</code>\n"
         f"<b>{E('cal')} Created:</b> <code>{dt(b['created'])}</code>\n"
         f"<b>{E('zap')} Auto-restart:</b> <code>{'ON' if b.get('auto', True) else 'OFF'}</code>")
    if viewer == OWNER and b["uid"] != OWNER:
        t += f"\n<b>{E('user')} Owner:</b> <code>{esc(uname(b['uid']))} ({b['uid']})</code>"
    t += "</blockquote>"
    toggle = B("Stop", f"sp:{n}", "stop", "danger") if a else B("Start", f"go:{n}", "play", "success")
    own = b["uid"] == viewer
    rows = [[toggle, B("Restart", f"rs:{n}", "re", "primary")],
            [B("Logs", f"lg:{n}", "log"), B("Refresh", f"b:{n}", "stat")],
            [B("Update file", f"uf:{n}", "edit"), B("Get file", f"df:{n}", "dl")],
            [B("Packages", f"rq:{n}", "pkg"), B("Env vars", f"ev:{n}", "lock")],
            [B("Settings", f"se:{n}", "gear"), B("Delete", f"del:{n}", "trash", "danger")],
            [B("My bots" if own else "Back", "l:m:0" if own else f"l:u{b['uid']}:0", "back")]]
    return t, rows


def list_screen(uid, scope, page):
    if scope == "m": ids, title, back = ubots(uid), "MY BOTS", "home"
    elif scope == "a": ids, title, back = list(reg["bots"]), "ALL BOTS", "adm"
    else:
        tu = int(scope[1:]); ids, title, back = ubots(tu), f"BOTS • {uname(tu)}", f"uv:{tu}"
    ids = sorted(ids, key=int)
    per = 6
    pages = max(1, (len(ids) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    if not ids:
        t = f"{E('bot')} <b>{esc(title)}</b>\n<blockquote><b>{E('info')} Abhi koi bot nahi hai.</b></blockquote>"
        rows = []
        if scope == "m": rows.append([B("Host new bot", "new", "plus", "success")])
        rows.append([B("Back", back, "back")])
        return t, rows
    rows = [[B(f"bot[{i}] • {reg['bots'][i]['name'][:18]}", f"b:{i}", "on" if alive(int(i)) else "off")]
            for i in ids[page * per:(page + 1) * per]]
    nav = []
    if page > 0: nav.append(B("Prev", f"l:{scope}:{page - 1}", "back"))
    if page < pages - 1: nav.append(B("Next", f"l:{scope}:{page + 1}", "arrow"))
    if nav: rows.append(nav)
    rows.append([B("Back", back, "back")])
    live = sum(1 for i in ids if alive(int(i)))
    t = (f"{E('bot')} <b>{esc(title)}</b>\n<blockquote>"
         f"<b>{E('stat')} Total:</b> <code>{len(ids)}</code>  <b>{E('on')} Live:</b> <code>{live}</code>  "
         f"<b>Page:</b> <code>{page + 1}/{pages}</code></blockquote>")
    return t, rows


def logs_screen(n):
    log = fulllog(n)
    body = esc(tail(log, 2400)) if log.strip() else "(abhi koi output nahi)"
    t = f"{E('log')} <b>LOGS</b> • <code>bot[{n}]</code> {E('on' if alive(n) else 'off')}\n<pre>{body}</pre>"
    rows = [[B("Refresh", f"lg:{n}", "re"), B("Download", f"lgd:{n}", "dl")],
            [B("Clear", f"lgc:{n}", "trash", "danger"), B("Panel", f"b:{n}", "back")]]
    return t, rows


def env_screen(n):
    env = reg["bots"][str(n)].get("env", {})
    lines = "\n".join(f"<b>{E('lock')}</b> <code>{esc(k)}</code> = <code>••••</code>" for k in env) or "<code>—</code>"
    t = (f"{E('lock')} <b>ENV VARIABLES</b> • <code>bot[{n}]</code>\n"
         f"<blockquote>{lines}</blockquote>\n"
         f"<b>{E('info')} Values secure rehti hain — sirf KEY dikhti hai.</b>")
    rows = [[B(f"Delete {k[:20]}", f"ed:{n}:{i}", "trash")] for i, k in enumerate(env)]
    rows.append([B("Add / update", f"ea:{n}", "plus", "success"), B("Export .env", f"ex:{n}", "dl")])
    rows.append([B("Panel", f"b:{n}", "back")])
    return t, rows


def req_screen(n):
    req = reqlines(reg["bots"][str(n)])
    body = "\n".join(f"<b>{E('pkg')}</b> <code>{esc(l)}</code>" for l in req) or "<code>—</code>"
    t = f"{E('pkg')} <b>PACKAGES</b> • <code>bot[{n}]</code>\n<blockquote>{body}</blockquote>"
    rows = [[B("Add / update", f"ur:{n}", "plus", "success"), B("Clear list", f"rc:{n}", "trash")],
            [B("Panel", f"b:{n}", "back")]]
    return t, rows


def settings_screen(n):
    b = reg["bots"][str(n)]
    ev = b.get("every", 0)
    ar = "ON" if b.get("auto", True) else "OFF"
    nt = "ON" if b.get("notify", True) else "OFF"
    sc = f"{ev}h" if ev else "OFF"
    t = (f"{E('gear')} <b>SETTINGS</b> • <code>bot[{n}]</code>\n<blockquote>"
         f"<b>{E('re')} Auto-restart:</b> <code>{ar}</code>\n"
         f"<b>{E('cal')} Scheduled restart:</b> <code>{sc}</code>\n"
         f"<b>{E('bell')} Crash alerts:</b> <code>{nt}</code>\n"
         f"<b>{E('edit')} Name:</b> <code>{esc(b['name'])}</code></blockquote>")
    rows = [[B(f"Auto-restart: {ar}", f"ar:{n}", "re")],
            [B(f"Schedule: {sc}", f"sc:{n}", "cal")],
            [B(f"Alerts: {nt}", f"nt:{n}", "bell")],
            [B("Rename", f"rn:{n}", "edit")]]
    if b.get("prev"): rows.append([B("Rollback file", f"rb:{n}", "re", "primary")])
    rows.append([B("Panel", f"b:{n}", "back")])
    return t, rows


def admin_screen():
    us = reg["users"]; bs = reg["bots"]
    banned = sum(1 for u in us.values() if u.get("banned"))
    live = sum(1 for i in bs if alive(int(i)))
    pend = sum(1 for b in bs.values() if b.get("pending"))
    ram, ld = sysinfo(); cfg = reg["cfg"]
    mt = "ON" if cfg["maint"] else "OFF"
    ap = "ON" if cfg["approval"] else "OFF"
    t = (f"{E('crown')} <b>ADMIN PANEL</b>\n<blockquote>"
         f"<b>{E('user')} Users:</b> <code>{len(us)}</code> (banned <code>{banned}</code>)\n"
         f"<b>{E('bot')} Bots:</b> <code>{len(bs)}</code> • live <code>{live}</code> • pending <code>{pend}</code>\n"
         f"<b>{E('stat')} RAM:</b> <code>{ram}</code>  <b>Load:</b> <code>{ld}</code>\n"
         f"<b>{E('clock')} Uptime:</b> <code>{dur(time.time() - T0)}</code></blockquote>\n"
         f"<blockquote><b>{E('gear')} Maintenance:</b> <code>{mt}</code>\n"
         f"<b>{E('shield')} Approval:</b> <code>{ap}</code>\n"
         f"<b>{E('gem')} Default limit:</b> <code>{cfg['limit']}</code></blockquote>")
    rows = [[B("Users", "ad:u:0", "user", "primary"), B("All bots", "l:a:0", "bot", "primary")],
            [B("Broadcast", "ad:b", "mega"), B(f"Pending ({pend})", "ad:q", "clock")],
            [B(f"Maintenance: {mt}", "ad:m", "gear")],
            [B(f"Approval: {ap}", "ad:p", "shield")],
            [B("Limit -", "ad:l:m", "gem"), B("Limit +", "ad:l:p", "gem")],
            [B("Stop all", "ad:sa", "stop", "danger"), B("Start all", "ad:ra", "play", "success")],
            [B("Refresh", "adm", "re"), B("Home", "home", "home")]]
    return t, rows


def users_screen(page):
    ids = sorted(reg["users"], key=lambda x: reg["users"][x].get("joined", 0), reverse=True)
    per = 7
    pages = max(1, (len(ids) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = []
    for i in ids[page * per:(page + 1) * per]:
        u = reg["users"][i]
        rows.append([B(f"{str(u.get('name', i))[:16]} • {len(ubots(int(i)))}", f"uv:{i}",
                       "ban" if u.get("banned") else "user")])
    nav = []
    if page > 0: nav.append(B("Prev", f"ad:u:{page - 1}", "back"))
    if page < pages - 1: nav.append(B("Next", f"ad:u:{page + 1}", "arrow"))
    if nav: rows.append(nav)
    rows.append([B("Back", "adm", "back")])
    t = (f"{E('user')} <b>USERS</b>\n<blockquote><b>Total:</b> <code>{len(ids)}</code>  "
         f"<b>Page:</b> <code>{page + 1}/{pages}</code></blockquote>")
    return t, rows


def userview_screen(tu):
    u = reg["users"].get(str(tu), {})
    bn = u.get("banned")
    t = (f"{E('user')} <b>USER</b>\n<blockquote>"
         f"<b>{E('pin')} Name:</b> <code>{esc(u.get('name', tu))}</code>\n"
         f"<b>{E('link')} User:</b> <code>@{esc(u.get('un') or '-')}</code>\n"
         f"<b>{E('star')} ID:</b> <code>{tu}</code>\n"
         f"<b>{E('cal')} Joined:</b> <code>{dt(u.get('joined', time.time()))}</code>\n"
         f"<b>{E('bot')} Bots:</b> <code>{len(ubots(tu))}/{ulimit(tu)}</code>\n"
         f"<b>{E('ban')} Banned:</b> <code>{'YES' if bn else 'NO'}</code></blockquote>")
    rows = [[B("Bots", f"l:u{tu}:0", "bot", "primary"), B("Message", f"um:{tu}", "mail")],
            [B("Limit -", f"ul:{tu}:m", "gem"), B("Limit +", f"ul:{tu}:p", "gem")],
            [B("Unban" if bn else "Ban", f"ub:{tu}", "ok" if bn else "ban", "success" if bn else "danger")],
            [B("Back", "ad:u:0", "back")]]
    return t, rows


def env_prompt():
    return (f"{E('lock')} <b>STEP 3/3 • ENV</b>\n<blockquote>"
            f"<b>Env variables bhejo, har line</b> <code>KEY=VALUE</code>\n"
            f"<code>BOT_TOKEN=123:ABC</code>\n<b>Ya</b> <code>.env</code> <b>file bhejo.</b>\n"
            f"<b>Nahi chahiye to Skip dabao.</b></blockquote>")


def req_prompt():
    return (f"{E('pkg')} <b>STEP 2/3 • PACKAGES</b>\n<blockquote>"
            f"<b>requirements.txt bhejo ya likho:</b> <code>qrcode pillow</code>\n"
            f"<b>Nahi chahiye to Skip dabao.</b></blockquote>")


# ---------------------------------------------------------------------
#  launch (with Successfully / Failed report) + approval
# ---------------------------------------------------------------------
async def launch(bot, chat_id, n, msg=None, verb="Start"):
    b = reg["bots"].get(str(n))
    if not b or n in busy: return
    busy.add(n)
    try:
        prog = (f"{E('clock')} <b>bot[{n}] {verb} ho raha hai…</b>\n"
                f"<blockquote><b>Startup check chal raha hai (~7s)</b></blockquote>")
        if msg:
            try: await tg(msg.edit_text, None, text=prog)
            except Exception: msg = None
        if not msg: msg = await tg(bot.send_message, None, chat_id=chat_id, text=prog)
        code = None
        if not os.path.exists(pyp(n)):
            ok, log = False, "main.py missing — file dobara upload karo"
        else:
            run(n); b["run"] = True
            for _ in range(7):
                await asyncio.sleep(1)
                if not alive(n): break
            ok = alive(n); log = newlog(n)
            if not ok: code = procs[n].returncode if n in procs else None
        bad = bool(ERR.search(log))
        panel_row = [B("Panel", f"b:{n}", "bot", "primary")]
        if ok and not bad:
            t = (f"{E('party')} <b>SUCCESSFULLY LIVE!</b>\n<blockquote>"
                 f"<b>{E('bot')} Bot:</b> <code>bot[{n}] • {esc(b['name'])}</code>\n"
                 f"<b>{E('on')} Status:</b> <code>RUNNING</code>\n"
                 f"<b>{E('zap')} PID:</b> <code>{procs[n].pid}</code>\n"
                 f"<b>{E('hundred')} Check:</b> <code>No error in startup logs</code></blockquote>")
            rows = [[B("Logs", f"lg:{n}", "log"), *panel_row]]
        elif ok:
            t = (f"{E('warn')} <b>RUNNING WITH ERRORS</b>\n<blockquote>"
                 f"<b>{E('on')} Status:</b> <code>RUNNING</code> (startup logs mein error mila)\n"
                 f"<b>{E('bulb')} Hint:</b> <code>{esc(diagnose(log))}</code></blockquote>\n"
                 f"<pre>{esc(tail(log, 1200))}</pre>")
            rows = [[B("Logs", f"lg:{n}", "log"), B("Env vars", f"ev:{n}", "lock")], panel_row]
        else:
            stop(n); b["run"] = False; b["crashes"] = b.get("crashes", 0) + 1
            t = (f"{E('siren')} <b>FAILED TO START</b>\n<blockquote>"
                 f"<b>{E('off')} Status:</b> <code>STOPPED</code>\n"
                 f"<b>{E('no')} Exit code:</b> <code>{code}</code>\n"
                 f"<b>{E('bulb')} Hint:</b> <code>{esc(diagnose(log))}</code></blockquote>\n"
                 f"<pre>{esc(tail(log, 1500)) or '(no output)'}</pre>")
            rows = [[B("Update file", f"uf:{n}", "edit"), B("Env vars", f"ev:{n}", "lock")],
                    [B("Packages", f"rq:{n}", "pkg"), B("Retry", f"go:{n}", "re", "primary")], panel_row]
        await save(bot)
        try: await tg(msg.edit_text, rows, text=t)
        except Exception: await tg(bot.send_message, rows, chat_id=chat_id, text=t)
    finally:
        busy.discard(n)


async def request_start(bot, chat_id, n, msg=None, verb="Start"):
    b = reg["bots"][str(n)]
    if b["uid"] != OWNER:
        if not reg["cfg"]["approval"]:
            b["approved"] = True
        elif not b.get("approved"):
            stop(n); b["run"] = False
            already = b.get("pending")
            b["pending"] = True; b["rejected"] = False
            await save(bot)
            t = (f"{E('shield')} <b>APPROVAL REQUIRED</b>\n<blockquote>"
                 f"<b>{E('clock')} bot[{n}]</b> owner approval ke liye bheja gaya hai.\n"
                 f"<b>Approve hote hi bot khud start ho jayega.</b></blockquote>")
            rows = [[B("Panel", f"b:{n}", "bot")]]
            if msg:
                try: await tg(msg.edit_text, rows, text=t)
                except Exception: await tg(bot.send_message, rows, chat_id=chat_id, text=t)
            else:
                await tg(bot.send_message, rows, chat_id=chat_id, text=t)
            if already: return
            fl = ", ".join(flags(n)) or "none"
            size = os.path.getsize(pyp(n)) // 1024 if os.path.exists(pyp(n)) else 0
            await ping(bot, OWNER,
                       f"{E('shield')} <b>APPROVAL REQUEST</b>\n<blockquote>"
                       f"<b>{E('user')} User:</b> <code>{esc(uname(b['uid']))} ({b['uid']})</code>\n"
                       f"<b>{E('bot')} Bot:</b> <code>bot[{n}] • {esc(b['name'])}</code>\n"
                       f"<b>{E('file')} Size:</b> <code>{size} KB</code>\n"
                       f"<b>{E('warn')} Flags:</b> <code>{esc(fl)}</code></blockquote>",
                       [[B("Approve", f"ap:{n}", "ok", "success"), B("Reject", f"rj:{n}", "no", "danger")],
                        [B("Get file", f"df:{n}", "dl"), B("Panel", f"b:{n}", "bot")]])
            return
    b["pending"] = False
    await launch(bot, chat_id, n, msg, verb)


# ---------------------------------------------------------------------
#  gate (registration / ban / maintenance)
# ---------------------------------------------------------------------
async def gate(u, c):
    user = u.effective_user
    if not user or not u.effective_chat or u.effective_chat.type != "private": return False
    uid = user.id
    rec = reg["users"].get(str(uid))
    if not rec:
        reg["users"][str(uid)] = {"name": user.full_name, "un": user.username or "",
                                  "joined": int(time.time())}
        asyncio.create_task(save(c.bot))
        if uid != OWNER:
            await ping(c.bot, OWNER, f"{E('new')} <b>NEW USER</b>\n<blockquote>"
                                     f"<b>{E('user')}</b> <code>{esc(user.full_name)}</code>\n"
                                     f"<b>{E('link')}</b> <code>@{esc(user.username or '-')}</code>\n"
                                     f"<b>{E('star')}</b> <code>{uid}</code></blockquote>",
                       [[B("View user", f"uv:{uid}", "user")]])
    else:
        rec["name"] = user.full_name; rec["un"] = user.username or ""
    if uid == OWNER: return True
    if rec and rec.get("banned"):
        await show(u, c, f"{E('ban')} <b>ACCESS BLOCKED</b>\n<blockquote><b>Tumhe is host se ban kar diya gaya hai.</b></blockquote>")
        return False
    if reg["cfg"]["maint"]:
        await show(u, c, f"{E('gear')} <b>MAINTENANCE</b>\n<blockquote><b>Host abhi maintenance mein hai. Thodi der baad aao.</b></blockquote>")
        return False
    return True


# ---------------------------------------------------------------------
#  commands
# ---------------------------------------------------------------------
async def cmd_menu(u, c):
    if not await gate(u, c): return
    c.user_data.pop("st", None)
    t, r = home_screen(u.effective_user.id)
    await show(u, c, t, r)


async def cmd_help(u, c):
    if not await gate(u, c): return
    t, r = help_screen(); await show(u, c, t, r)


async def cmd_mybots(u, c):
    if not await gate(u, c): return
    t, r = list_screen(u.effective_user.id, "m", 0); await show(u, c, t, r)


async def cmd_admin(u, c):
    if u.effective_user.id != OWNER: return
    t, r = admin_screen(); await show(u, c, t, r)


async def cmd_sh(u, c):
    if u.effective_user.id != OWNER: return
    cmd = (u.message.text or "").partition(" ")[2].strip()
    if not cmd:
        return await say(c, OWNER, f"{E('info')} <b>Usage:</b> <code>/sh ls -la</code>")
    p = None
    try:
        p = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE,
                                                  stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), 30)
        txt = out.decode(errors="ignore")
    except asyncio.TimeoutError:
        try: p.kill()
        except Exception: pass
        txt = "(timeout 30s)"
    except Exception as e:
        txt = str(e)
    await say(c, OWNER, f"{E('bot')} <b>SHELL</b>\n<pre>{esc(tail(txt, 3500)) or '(no output)'}</pre>")


# ---------------------------------------------------------------------
#  buttons
# ---------------------------------------------------------------------
BOT_ACTS = {"b", "go", "sp", "rs", "lg", "lgd", "lgc", "uf", "df", "rq", "ur", "rc", "ev", "ea", "ed", "ex",
            "se", "ar", "sc", "nt", "rn", "rb", "del", "dok"}
OWN_ACTS = {"adm", "ad", "uv", "ub", "ul", "um", "ap", "rj", "bcok"}


async def on_button(u, c):
    q = u.callback_query
    try: await q.answer()
    except Exception: pass
    if not await gate(u, c): return
    uid = q.from_user.id
    bot = c.bot
    act, *a = q.data.split(":")
    st = c.user_data.get("st")
    if act != "sk": c.user_data.pop("st", None)
    chat = u.effective_chat.id

    if act in OWN_ACTS and uid != OWNER: return
    n = int(a[0]) if (act in BOT_ACTS or act in ("ap", "rj")) and a and a[0].isdigit() else None
    if act in BOT_ACTS and (n is None or not can(uid, n)):
        return await show(u, c, f"{E('no')} <b>Ye bot exist nahi karta ya access nahi hai.</b>",
                          [[B("Home", "home", "home")]])

    # ---------- user menu ----------
    if act == "home":
        t, r = home_screen(uid); await show(u, c, t, r)

    elif act == "me":
        t, r = profile_screen(uid); await show(u, c, t, r)

    elif act == "help":
        t, r = help_screen(); await show(u, c, t, r)

    elif act == "new":
        if len(ubots(uid)) >= ulimit(uid):
            return await show(u, c, f"{E('ban')} <b>LIMIT REACHED</b>\n<blockquote>"
                                    f"<b>Tum max</b> <code>{ulimit(uid)}</code> <b>bot host kar sakte ho.</b>\n"
                                    f"<b>Naya host karne ke liye purana delete karo.</b></blockquote>",
                              [[B("My bots", "l:m:0", "bot", "primary")], [B("Home", "home", "home")]])
        c.user_data["st"] = ("py_new",)
        await show(u, c, f"{E('file')} <b>STEP 1/3 • FILE</b>\n<blockquote>"
                         f"<b>Apni bot ki</b> <code>.py</code> <b>file bhejo</b>\n"
                         f"<b>Max size:</b> <code>{MAX_KB} KB</code>  <b>Syntax auto-check hoga</b></blockquote>",
                   [[B("Cancel", "home", "no", "danger")]])

    elif act == "l":
        t, r = list_screen(uid, a[0], int(a[1])); await show(u, c, t, r)

    # ---------- bot actions ----------
    elif act == "b":
        t, r = panel(n, uid); await show(u, c, t, r)

    elif act in ("go", "rs"):
        await request_start(bot, chat, n, q.message, "Start" if act == "go" else "Restart")

    elif act == "sp":
        stop(n); reg["bots"][str(n)]["run"] = False; await save(bot)
        t, r = panel(n, uid); await show(u, c, t, r)

    elif act == "lg":
        t, r = logs_screen(n); await show(u, c, t, r)

    elif act == "lgd":
        if not fulllog(n).strip():
            return await say(c, chat, f"{E('info')} <b>Log khali hai.</b>")
        await tg(bot.send_document, None, chat_id=chat, document=open(logp(n), "rb"),
                 filename=f"bot{n}.log", caption=f"{E('log')} <b>Full log • bot[{n}]</b>")

    elif act == "lgc":
        try: open(logp(n), "w").close()
        except Exception: pass
        meta.setdefault(n, {})["off"] = 0
        t, r = logs_screen(n); await show(u, c, t, r)

    elif act == "uf":
        c.user_data["st"] = ("py_upd", n)
        await show(u, c, f"{E('edit')} <b>UPDATE FILE</b>\n<blockquote><b>bot[{n}] ki nayi</b> <code>.py</code> "
                         f"<b>file bhejo.</b>\n<b>Purani file Rollback ke liye save rahegi.</b></blockquote>",
                   [[B("Cancel", f"b:{n}", "no", "danger")]])

    elif act == "df":
        b = reg["bots"][str(n)]
        if not os.path.exists(pyp(n)):
            return await say(c, chat, f"{E('no')} <b>File missing hai.</b>")
        fn = re.sub(r"[^\w.\-]", "_", b["name"]); fn = fn if fn.endswith(".py") else fn + ".py"
        await tg(bot.send_document, None, chat_id=chat, document=open(pyp(n), "rb"), filename=fn,
                 caption=f"{E('file')} <b>Current file • bot[{n}]</b>")

    elif act == "rq":
        t, r = req_screen(n); await show(u, c, t, r)

    elif act == "ur":
        c.user_data["st"] = ("req_upd", n)
        await show(u, c, f"{E('pkg')} <b>ADD / UPDATE PACKAGES</b>\n<blockquote>"
                         f"<b>requirements.txt bhejo ya likho:</b> <code>qrcode pillow requests==2.31.0</code>\n"
                         f"<b>Naye packages purani list mein add/update honge.</b></blockquote>",
                   [[B("Cancel", f"rq:{n}", "no", "danger")]])

    elif act == "rc":
        reg["bots"][str(n)]["req"] = ""; await save(bot)
        t, r = req_screen(n); await show(u, c, t, r)

    elif act == "ev":
        t, r = env_screen(n); await show(u, c, t, r)

    elif act == "ea":
        c.user_data["st"] = ("env_add", n)
        await show(u, c, f"{E('lock')} <b>ADD / UPDATE ENV</b>\n<blockquote>"
                         f"<b>Har line mein</b> <code>KEY=VALUE</code> <b>bhejo ya</b> <code>.env</code> <b>file.</b>\n"
                         f"<code>BOT_TOKEN=123:ABC</code>\n<code>API_KEY=xyz</code>\n"
                         f"<b>Same KEY dobara = update. Tumhara message turant delete ho jayega.</b></blockquote>",
                   [[B("Cancel", f"ev:{n}", "no", "danger")]])

    elif act == "ed":
        b = reg["bots"][str(n)]
        keys = list(b.get("env", {}))
        if len(a) > 1 and a[1].isdigit() and int(a[1]) < len(keys):
            b["env"].pop(keys[int(a[1])]); await save(bot)
            if b.get("run"):
                asyncio.create_task(launch(bot, chat, n, None, "Restart"))
        t, r = env_screen(n); await show(u, c, t, r)

    elif act == "ex":
        env = reg["bots"][str(n)].get("env", {})
        if not env: return await say(c, chat, f"{E('info')} <b>Env khali hai.</b>")
        data = "\n".join(f"{k}={v}" for k, v in env.items()).encode()
        await tg(bot.send_document, None, chat_id=chat, document=data, filename=".env",
                 caption=f"{E('lock')} <b>.env • bot[{n}]</b>\n<b>{E('warn')} Secret hai — share mat karo.</b>")

    # ---------- settings ----------
    elif act == "se":
        t, r = settings_screen(n); await show(u, c, t, r)

    elif act in ("ar", "nt", "sc"):
        b = reg["bots"][str(n)]
        if act == "ar": b["auto"] = not b.get("auto", True)
        elif act == "nt": b["notify"] = not b.get("notify", True)
        else:
            cyc = [0, 6, 12, 24]
            cur = b.get("every", 0)
            b["every"] = cyc[(cyc.index(cur) + 1) % len(cyc)] if cur in cyc else 0
        await save(bot)
        t, r = settings_screen(n); await show(u, c, t, r)

    elif act == "rn":
        c.user_data["st"] = ("rn", n)
        await show(u, c, f"{E('edit')} <b>RENAME</b>\n<blockquote><b>bot[{n}] ka naya naam likho (max 40).</b></blockquote>",
                   [[B("Cancel", f"se:{n}", "no", "danger")]])

    elif act == "rb":
        b = reg["bots"][str(n)]; pv = b.get("prev")
        if not pv: return
        try:
            f = await bot.get_file(pv["file_id"]); data = bytes(await f.download_as_bytearray())
        except Exception as e:
            return await say(c, chat, f"{E('no')} <b>Rollback fail:</b> <code>{esc(e)}</code>")
        open(pyp(n), "wb").write(data)
        b["prev"] = {"file_id": b["file_id"], "name": b["name"]}
        b["file_id"], b["name"] = pv["file_id"], pv["name"]
        b["approved"] = b["uid"] == OWNER
        await save(bot)
        if b.get("run"):
            await request_start(bot, chat, n, q.message, "Restart")
        else:
            t, r = settings_screen(n); await show(u, c, t, r)

    elif act == "del":
        await show(u, c, f"{E('warn')} <b>DELETE bot[{n}]?</b>\n<blockquote><b>File, env, logs sab delete ho jayenge.</b></blockquote>",
                   [[B("Haan, delete", f"dok:{n}", "ok", "danger"), B("Nahi", f"b:{n}", "no")]])

    elif act == "dok":
        stop(n); reg["bots"].pop(str(n), None)
        shutil.rmtree(bdir(n), ignore_errors=True)
        await save(bot)
        await show(u, c, f"{E('trash')} <b>bot[{n}] delete ho gaya.</b>",
                   [[B("My bots", "l:m:0", "bot"), B("Home", "home", "home")]])

    # ---------- onboarding skips ----------
    elif act == "sk":
        if a and a[0] == "req" and st and st[0] == "req_new":
            c.user_data["st"] = ("env_new", st[1])
            await show(u, c, env_prompt(), [[B("Skip", "sk:env", "arrow")]])
        elif a and a[0] == "env" and st and st[0] == "env_new":
            c.user_data.pop("st", None)
            await request_start(bot, chat, st[1], q.message)

    # ---------- approvals ----------
    elif act == "ap" and n is not None and str(n) in reg["bots"]:
        b = reg["bots"][str(n)]
        b["approved"] = True; b["pending"] = False; await save(bot)
        await show(u, c, f"{E('ok')} <b>bot[{n}] approved.</b>", [[B("Panel", f"b:{n}", "bot")]])
        await ping(bot, b["uid"], f"{E('ok')} <b>Tumhara bot[{n}] approve ho gaya!</b>")
        asyncio.create_task(launch(bot, b["uid"], n))

    elif act == "rj" and n is not None and str(n) in reg["bots"]:
        b = reg["bots"][str(n)]
        b["pending"] = False; b["rejected"] = True; b["run"] = False; await save(bot)
        await show(u, c, f"{E('no')} <b>bot[{n}] rejected.</b>", [[B("Panel", f"b:{n}", "bot")]])
        await ping(bot, b["uid"], f"{E('no')} <b>Tumhara bot[{n}] reject ho gaya.</b>\n"
                                  f"<b>File update karke dobara try karo.</b>")

    # ---------- admin ----------
    elif act == "adm":
        t, r = admin_screen(); await show(u, c, t, r)

    elif act == "ad":
        sub = a[0]
        if sub == "u":
            t, r = users_screen(int(a[1])); await show(u, c, t, r)
        elif sub == "m":
            reg["cfg"]["maint"] = not reg["cfg"]["maint"]; await save(bot)
            t, r = admin_screen(); await show(u, c, t, r)
        elif sub == "p":
            reg["cfg"]["approval"] = not reg["cfg"]["approval"]; await save(bot)
            t, r = admin_screen(); await show(u, c, t, r)
        elif sub == "l":
            cur = reg["cfg"]["limit"]
            reg["cfg"]["limit"] = min(20, cur + 1) if a[1] == "p" else max(1, cur - 1); await save(bot)
            t, r = admin_screen(); await show(u, c, t, r)
        elif sub == "b":
            c.user_data["st"] = ("bc",)
            await show(u, c, f"{E('mega')} <b>BROADCAST</b>\n<blockquote><b>Sab users ko bhejne wala message likho.</b></blockquote>",
                       [[B("Cancel", "adm", "no", "danger")]])
        elif sub == "q":
            ids = [i for i, b in reg["bots"].items() if b.get("pending")]
            rows = [[B(f"bot[{i}] • {uname(reg['bots'][i]['uid'])[:14]}", f"b:{i}", "clock")] for i in ids]
            rows.append([B("Back", "adm", "back")])
            await show(u, c, f"{E('clock')} <b>PENDING APPROVALS</b>\n<blockquote><b>Total:</b> <code>{len(ids)}</code></blockquote>", rows)
        elif sub == "sa":
            cnt = 0
            for i, b in reg["bots"].items():
                if alive(int(i)): stop(int(i)); b["run"] = False; cnt += 1
            await save(bot)
            await show(u, c, f"{E('stop')} <b>{cnt} bots stop ho gaye.</b>", [[B("Back", "adm", "back")]])
        elif sub == "ra":
            cnt = 0
            for i, b in reg["bots"].items():
                if not alive(int(i)) and not b.get("pending") and os.path.exists(pyp(int(i))):
                    run(int(i)); b["run"] = True; cnt += 1
            await save(bot)
            await show(u, c, f"{E('play')} <b>{cnt} bots start ho gaye.</b>", [[B("Back", "adm", "back")]])

    elif act == "uv":
        t, r = userview_screen(int(a[0])); await show(u, c, t, r)

    elif act == "ub":
        tu = int(a[0])
        if tu != OWNER and str(tu) in reg["users"]:
            usr = reg["users"][str(tu)]
            usr["banned"] = not usr.get("banned")
            if usr["banned"]:
                for i in ubots(tu):
                    stop(int(i)); reg["bots"][i]["run"] = False
                await ping(bot, tu, f"{E('ban')} <b>Tumhe host se ban kar diya gaya hai.</b>")
            else:
                await ping(bot, tu, f"{E('ok')} <b>Tumhara ban hat gaya hai.</b>")
            await save(bot)
        t, r = userview_screen(tu); await show(u, c, t, r)

    elif act == "ul":
        tu = int(a[0]); usr = reg["users"].get(str(tu))
        if usr is not None:
            cur = usr.get("limit", reg["cfg"]["limit"])
            usr["limit"] = min(50, cur + 1) if a[1] == "p" else max(0, cur - 1); await save(bot)
        t, r = userview_screen(tu); await show(u, c, t, r)

    elif act == "um":
        c.user_data["st"] = ("dm", int(a[0]))
        await show(u, c, f"{E('mail')} <b>MESSAGE USER</b>\n<blockquote><b>Message likho.</b></blockquote>",
                   [[B("Cancel", f"uv:{a[0]}", "no", "danger")]])

    elif act == "bcok":
        txt = c.user_data.pop("bc_text", None)
        if not txt: return
        sent = fail = 0
        await show(u, c, f"{E('clock')} <b>Broadcast chal raha hai…</b>")
        for i, usr in list(reg["users"].items()):
            if usr.get("banned"): continue
            r = await ping(bot, int(i), f"{E('mega')} <b>ANNOUNCEMENT</b>\n<blockquote><b>{esc(txt)}</b></blockquote>")
            if r: sent += 1
            else: fail += 1
            await asyncio.sleep(0.05)
        await say(c, chat, f"{E('ok')} <b>Broadcast done</b>\n<blockquote><b>Sent:</b> <code>{sent}</code>  "
                           f"<b>Failed:</b> <code>{fail}</code></blockquote>", [[B("Admin", "adm", "crown")]])


# ---------------------------------------------------------------------
#  file / text input
# ---------------------------------------------------------------------
async def read_doc(c, d, limit):
    if d.file_size and d.file_size > limit: return None
    f = await c.bot.get_file(d.file_id)
    return bytes(await f.download_as_bytearray())


async def handle_py(u, c, uid, st, d):
    kind = st[0]
    back = "home" if kind == "py_new" else f"b:{st[1]}"
    cancel = [[B("Cancel", back, "no", "danger")]]
    if not d.file_name or not d.file_name.lower().endswith(".py"):
        return await say(c, uid, f"{E('no')} <b>Sirf</b> <code>.py</code> <b>file bhejo.</b>", cancel)
    data = await read_doc(c, d, MAX_KB * 1024)
    if data is None:
        return await say(c, uid, f"{E('no')} <b>File badi hai (max {MAX_KB} KB).</b>", cancel)
    err = check_py(data)
    if err:
        return await say(c, uid, f"{E('siren')} <b>SYNTAX ERROR</b>\n<blockquote><code>{esc(err)}</code></blockquote>\n"
                                 f"<b>Fix karke dobara bhejo.</b>", cancel)
    if kind == "py_new":
        if len(ubots(uid)) >= ulimit(uid):
            c.user_data.pop("st", None)
            return await say(c, uid, f"{E('ban')} <b>Limit reached.</b>", [[B("My bots", "l:m:0", "bot")]])
        n = reg["next"]; reg["next"] += 1
        os.makedirs(bdir(n), exist_ok=True)
        open(pyp(n), "wb").write(data)
        reg["bots"][str(n)] = {"uid": uid, "name": d.file_name, "file_id": d.file_id, "run": False,
                               "req": "", "env": {}, "auto": True, "every": 0, "notify": True,
                               "created": int(time.time()), "starts": 0, "crashes": 0,
                               "approved": uid == OWNER}
        c.user_data["st"] = ("req_new", n)
        await save(c.bot)
        await say(c, uid, f"{E('ok')} <b>File mil gayi</b> • <code>bot[{n}]</code>\n\n" + req_prompt(),
                  [[B("Skip", "sk:req", "arrow")]])
        if uid != OWNER:
            await ping(c.bot, OWNER, f"{E('bot')} <b>NEW BOT</b> • <code>bot[{n}]</code> by <code>{esc(uname(uid))}</code>",
                       [[B("Panel", f"b:{n}", "bot")]])
        return
    n = st[1]
    if not can(uid, n): return
    b = reg["bots"][str(n)]
    b["prev"] = {"file_id": b["file_id"], "name": b["name"]}
    open(pyp(n), "wb").write(data)
    b["file_id"], b["name"] = d.file_id, d.file_name
    b["approved"] = b["uid"] == OWNER
    b["pending"] = False; b["rejected"] = False
    c.user_data.pop("st", None)
    await save(c.bot)
    if b.get("run"):
        await say(c, uid, f"{E('ok')} <b>File update ho gayi</b> • <code>bot[{n}]</code>")
        await request_start(c.bot, uid, n, None, "Restart")
    else:
        await say(c, uid, f"{E('ok')} <b>File update ho gayi</b> • <code>bot[{n}]</code>",
                  [[B("Start", f"go:{n}", "play", "success"), B("Panel", f"b:{n}", "bot")]])


async def apply_req(u, c, uid, kind, n, raw):
    b = reg["bots"][str(n)]
    lines, bad = clean_req(raw, uid == OWNER)
    cancel = [[B("Cancel", f"b:{n}", "no", "danger")]]
    if bad:
        return await say(c, uid, f"{E('no')} <b>Invalid package line</b>\n<blockquote><code>{esc(', '.join(bad)[:300])}</code></blockquote>\n"
                                 f"<b>Sirf package naam / version allowed hai.</b>", cancel)
    if not lines:
        return await say(c, uid, f"{E('no')} <b>Koi package nahi mila. Dobara bhejo.</b>", cancel)
    merged = {req_name(l): l for l in reqlines(b)}
    for l in lines: merged[req_name(l)] = l
    text = "\n".join(merged.values())
    msg = await say(c, uid, f"{E('clock')} <b>Packages install ho rahe hain…</b>")
    ok, out = await asyncio.to_thread(pip_install, n, text)
    if not ok:
        return await say(c, uid, f"{E('siren')} <b>INSTALL FAILED</b>\n<pre>{esc(out)}</pre>\n"
                                 f"<b>Dobara bhejo ya cancel karo.</b>", cancel)
    b["req"] = text
    c.user_data.pop("st", None)
    await save(c.bot)
    if kind == "req_new":
        c.user_data["st"] = ("env_new", n)
        return await tg(msg.edit_text, [[B("Skip", "sk:env", "arrow")]],
                        text=f"{E('ok')} <b>Packages install ho gaye.</b>\n\n" + env_prompt())
    if b.get("run"):
        return await request_start(c.bot, uid, n, msg, "Restart")
    await tg(msg.edit_text, [[B("Packages", f"rq:{n}", "pkg"), B("Panel", f"b:{n}", "bot")]],
             text=f"{E('ok')} <b>Packages install ho gaye.</b>")


async def apply_env(u, c, uid, kind, n, text):
    pairs = parse_env(text)
    cancel = [[B("Cancel", f"b:{n}", "no", "danger")]]
    try: await u.message.delete()  # secrets chat mein na rahein
    except Exception: pass
    if not pairs:
        return await say(c, uid, f"{E('no')} <b>Koi valid</b> <code>KEY=VALUE</code> <b>nahi mili. Dobara bhejo.</b>", cancel)
    b = reg["bots"][str(n)]
    b.setdefault("env", {}).update(pairs)
    c.user_data.pop("st", None)
    await save(c.bot)
    keys = ", ".join(esc(k) for k in pairs)
    msg = await say(c, uid, f"{E('ok')} <b>{len(pairs)} env set hui</b>\n<blockquote><code>{keys}</code></blockquote>")
    if kind == "env_new":
        return await request_start(c.bot, uid, n, msg)
    if b.get("run"):
        return await request_start(c.bot, uid, n, msg, "Restart")
    t, r = env_screen(n)
    await say(c, uid, t, r)


async def on_doc(u, c):
    if not await gate(u, c): return
    uid = u.effective_user.id
    st = c.user_data.get("st")
    if not st:
        t, r = home_screen(uid)
        return await say(c, uid, f"{E('arrow')} <b>Pehle menu se option chuno.</b>\n\n" + t, r)
    d = u.message.document; kind = st[0]
    if kind in ("py_new", "py_upd"):
        return await handle_py(u, c, uid, st, d)
    if kind in ("req_new", "req_upd", "env_new", "env_add"):
        data = await read_doc(c, d, 200_000)
        if data is None: return await say(c, uid, f"{E('no')} <b>File badi hai.</b>")
        text = data.decode("utf-8", errors="ignore")
        if kind.startswith("req"): return await apply_req(u, c, uid, kind, st[1], text)
        return await apply_env(u, c, uid, kind, st[1], text)
    t, r = home_screen(uid)
    await say(c, uid, t, r)


async def on_text(u, c):
    if not await gate(u, c): return
    uid = u.effective_user.id
    st = c.user_data.get("st")
    text = u.message.text or ""
    if not st:
        t, r = home_screen(uid)
        return await say(c, uid, t, r)
    k = st[0]
    if k in ("req_new", "req_upd"):
        pk = [p for p in re.split(r"[,\s]+", text.strip()) if p]
        return await apply_req(u, c, uid, k, st[1], "\n".join(pk))
    if k in ("env_new", "env_add"):
        return await apply_env(u, c, uid, k, st[1], text)
    if k == "rn" and can(uid, st[1]):
        reg["bots"][str(st[1])]["name"] = text.strip()[:40] or "bot.py"
        c.user_data.pop("st", None); await save(c.bot)
        t, r = settings_screen(st[1]); return await say(c, uid, t, r)
    if k == "bc" and uid == OWNER:
        c.user_data["bc_text"] = text; c.user_data.pop("st", None)
        return await say(c, uid, f"{E('mega')} <b>PREVIEW</b>\n<blockquote><b>{esc(text)}</b></blockquote>\n"
                                 f"<b>Sab users ko bhejun?</b>",
                         [[B("Send", "bcok", "ok", "success"), B("Cancel", "adm", "no", "danger")]])
    if k == "dm" and uid == OWNER:
        c.user_data.pop("st", None)
        r = await ping(c.bot, st[1], f"{E('mail')} <b>MESSAGE FROM ADMIN</b>\n<blockquote><b>{esc(text)}</b></blockquote>")
        return await say(c, uid, f"{E('ok') if r else E('no')} <b>{'Bhej diya' if r else 'Send fail'}</b>",
                         [[B("User", f"uv:{st[1]}", "user")]])
    t, r = home_screen(uid)
    await say(c, uid, t, r)


# ---------------------------------------------------------------------
#  watchdog / boot
# ---------------------------------------------------------------------
async def watchdog(bot):
    await BOOTED.wait()
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for i, b in list(reg["bots"].items()):
            n = int(i)
            try:
                if not b.get("run") or b.get("pending") or n in busy: continue
                if alive(n):
                    ev = b.get("every", 0)
                    if ev and now - meta.get(n, {}).get("t0", now) > ev * 3600:
                        run(n)
                        if b.get("notify", True):
                            await ping(bot, b["uid"], f"{E('re')} <b>bot[{n}] scheduled restart ho gaya.</b>")
                    continue
                if not os.path.exists(pyp(n)): continue
                b["crashes"] = b.get("crashes", 0) + 1; b["last_crash"] = int(now)
                hist = [t for t in crashlog.get(n, []) if now - t < 300] + [now]
                crashlog[n] = hist
                snip = tail(newlog(n), 700)
                if not b.get("auto", True) or len(hist) >= 4:
                    stop(n); b["run"] = False
                    head = (f"{E('siren')} <b>bot[{n}] CRASH LOOP — band kar diya</b>" if len(hist) >= 4
                            else f"{E('siren')} <b>bot[{n}] crash hua</b> (auto-restart OFF)")
                    rows = [[B("Logs", f"lg:{n}", "log"), B("Panel", f"b:{n}", "bot")]]
                else:
                    run(n)
                    head = f"{E('re')} <b>bot[{n}] crash hua — auto-restart ho gaya</b>"
                    rows = [[B("Logs", f"lg:{n}", "log")]]
                await save(bot)
                if b.get("notify", True):
                    await ping(bot, b["uid"], f"{head}\n<blockquote><b>{E('bulb')}</b> <code>{esc(diagnose(snip))}</code></blockquote>\n"
                                              f"<pre>{esc(snip) or '(no output)'}</pre>", rows)
            except Exception as e:
                print("watchdog error:", e)


async def boot(app):
    bot = app.bot
    ok = 0
    for i, b in list(reg["bots"].items()):
        n = int(i)
        try:
            os.makedirs(bdir(n), exist_ok=True)
            f = await bot.get_file(b["file_id"])
            await f.download_to_drive(pyp(n))
            if b.get("req"):
                await asyncio.to_thread(pip_install, n, b["req"])
            if b.get("run") and not b.get("pending"):
                run(n)
            ok += 1
        except Exception as e:
            print("restore fail", n, e)
    BOOTED.set()
    await ping(bot, OWNER, f"{E('ok')} <b>HOST ONLINE</b>\n<blockquote><b>{E('bot')} Restored:</b> "
                           f"<code>{ok}/{len(reg['bots'])}</code> bots</blockquote>")


async def post_init(app):
    global reg, reg_msg_id, save_lock, BOOTED
    save_lock = asyncio.Lock()
    BOOTED = asyncio.Event()
    bot = app.bot
    loaded = None
    try:
        chat = await bot.get_chat(OWNER)
        pm = chat.pinned_message
        if pm:
            loaded = await load_msg(pm)
            if loaded is not None: reg_msg_id = pm.message_id
    except Exception as e:
        print("registry load error:", e)
    if loaded is None and os.environ.get("REG_MSG_ID", "").isdigit():
        try:
            fm = await bot.forward_message(OWNER, OWNER, int(os.environ["REG_MSG_ID"]))
            loaded = await load_msg(fm); reg_msg_id = int(os.environ["REG_MSG_ID"])
            await bot.delete_message(OWNER, fm.message_id)
        except Exception as e:
            print("REG_MSG_ID load error:", e)
    if loaded is not None:
        reg = loaded
    norm()
    asyncio.create_task(boot(app))
    asyncio.create_task(watchdog(bot))


async def on_error(u, c):
    print("ERROR:", repr(c.error))


def main():
    threading.Thread(target=web, daemon=True).start()
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init).build()
    P = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler(["start", "menu"], cmd_menu, filters=P))
    app.add_handler(CommandHandler("help", cmd_help, filters=P))
    app.add_handler(CommandHandler("mybots", cmd_mybots, filters=P))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=P))
    app.add_handler(CommandHandler("sh", cmd_sh, filters=P))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL & P, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & P, on_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
