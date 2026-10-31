import asyncio, json, os, re, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
DIR = "hosted"
os.makedirs(DIR, exist_ok=True)

# Registry is stored in a pinned message in your chat (Render disk is wiped on restart)
reg = {"next": 1, "bots": {}}
reg_msg_id = None
procs = {}


# ---------------- web server (Render needs an open port) ----------------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"alive")
    def log_message(self, *a): pass

def web():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Ping).serve_forever()


# ---------------- process helpers ----------------
def pyp(n): return f"{DIR}/bot{n}.py"
def logp(n): return f"{DIR}/bot{n}.log"
def reqp(n): return f"{DIR}/bot{n}_req.txt"

def alive(n):
    p = procs.get(n)
    return bool(p and p.poll() is None)

def stop(n):
    p = procs.pop(n, None)
    if p and p.poll() is None:
        p.terminate()
        try: p.wait(5)
        except Exception: p.kill()

def run(n):
    stop(n)
    if os.path.exists(logp(n)) and os.path.getsize(logp(n)) > 200_000:
        os.remove(logp(n))
    out = open(logp(n), "ab")
    procs[n] = subprocess.Popen([sys.executable, "-u", pyp(n)],
                                stdout=out, stderr=subprocess.STDOUT)

def pip_install(n, text):
    open(reqp(n), "w").write(text)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", reqp(n)],
                       capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-600:]

async def save(bot):
    global reg_msg_id
    text = "REGISTRY\n" + json.dumps(reg)
    try:
        if reg_msg_id:
            await bot.edit_message_text(text, chat_id=OWNER, message_id=reg_msg_id)
        else:
            m = await bot.send_message(OWNER, text)
            await bot.pin_chat_message(OWNER, m.message_id, disable_notification=True)
            reg_msg_id = m.message_id
    except BadRequest as e:
        if "not modified" not in str(e):
            print("save error:", e)


# ---------------- UI ----------------
def main_kb():
    return Kb([[Btn("➕ Host new bot", callback_data="new")],
               [Btn("🤖 My bots", callback_data="list")]])

def back_kb(n):
    return Kb([[Btn("⬅ Bot panel", callback_data=f"b:{n}")]])

def panel(n):
    b = reg["bots"][str(n)]
    req = ", ".join(b.get("req", "").split()) or "—"
    text = (f"🤖 bot[{n}]  •  {b['name']}\n"
            f"Status: {'🟢 Running' if alive(n) else '🔴 Stopped'}\n"
            f"Requirements: {req}")
    toggle = (Btn("⏹ Stop", callback_data=f"stop:{n}") if alive(n)
              else Btn("▶️ Start", callback_data=f"start:{n}"))
    kb = Kb([
        [toggle, Btn("🔄 Restart", callback_data=f"rs:{n}")],
        [Btn("📜 Logs", callback_data=f"log:{n}")],
        [Btn("📝 Update file", callback_data=f"upf:{n}"),
         Btn("📦 Update requirements", callback_data=f"upr:{n}")],
        [Btn("🗑 Delete", callback_data=f"del:{n}")],
        [Btn("⬅ My bots", callback_data="list")],
    ])
    return text, kb

async def show(u: Update, text, kb=None):
    if u.callback_query:
        try:
            await u.callback_query.edit_message_text(text, reply_markup=kb)
        except BadRequest as e:
            if "not modified" not in str(e):
                raise
    else:
        await u.message.reply_text(text, reply_markup=kb)

async def show_panel(u, n):
    t, k = panel(n)
    await show(u, t, k)


# ---------------- handlers ----------------
async def menu(u, c):
    if u.effective_user.id != OWNER: return
    c.user_data.pop("state", None)
    await show(u, "👋 Bot Host\nKya karna hai?", main_kb())

async def on_button(u, c):
    q = u.callback_query
    await q.answer()
    if q.from_user.id != OWNER: return
    d = q.data
    st = c.user_data.get("state")
    if d != "skip":
        c.user_data.pop("state", None)
    act, _, arg = d.partition(":")
    n = int(arg) if arg.isdigit() else None

    if d == "menu":
        await show(u, "👋 Bot Host\nKya karna hai?", main_kb())

    elif d == "new":
        c.user_data["state"] = ("py_new",)
        await show(u, "📄 Apni bot ki .py file bhejo.",
                   Kb([[Btn("❌ Cancel", callback_data="menu")]]))

    elif d == "list":
        if not reg["bots"]:
            return await show(u, "Abhi koi bot host nahi hai.",
                              Kb([[Btn("➕ Host new bot", callback_data="new")],
                                  [Btn("⬅ Back", callback_data="menu")]]))
        rows = [[Btn(f"{'🟢' if alive(int(i)) else '🔴'} bot[{i}] • {b['name']}",
                     callback_data=f"b:{i}")] for i, b in reg["bots"].items()]
        rows.append([Btn("⬅ Back", callback_data="menu")])
        await show(u, "🤖 Tumhare bots:", Kb(rows))

    elif d == "skip":
        if st and st[0] == "req_new":
            n = st[1]
            c.user_data.pop("state", None)
            run(n); reg["bots"][str(n)]["run"] = True; await save(c.bot)
            await show_panel(u, n)

    elif n is not None and str(n) not in reg["bots"]:
        await show(u, "Ye bot ab exist nahi karta.", Kb([[Btn("⬅ Back", callback_data="list")]]))

    elif act == "b":
        await show_panel(u, n)

    elif act == "start" or act == "rs":
        run(n); reg["bots"][str(n)]["run"] = True; await save(c.bot)
        await show_panel(u, n)

    elif act == "stop":
        stop(n); reg["bots"][str(n)]["run"] = False; await save(c.bot)
        await show_panel(u, n)

    elif act == "log":
        try: t = open(logp(n), errors="ignore").read()[-2500:] or "(abhi koi output nahi)"
        except FileNotFoundError: t = "(log file nahi mili)"
        await show(u, f"📜 bot[{n}] logs  {'🟢' if alive(n) else '🔴'}\n\n{t}",
                   Kb([[Btn("🔄 Refresh", callback_data=f"log:{n}")],
                       [Btn("⬅ Bot panel", callback_data=f"b:{n}")]]))

    elif act == "upf":
        c.user_data["state"] = ("py_upd", n)
        await show(u, f"📝 bot[{n}] ki nayi .py file bhejo.", back_kb(n))

    elif act == "upr":
        c.user_data["state"] = ("req_upd", n)
        await show(u, f"📦 bot[{n}] ke liye requirements.txt bhejo,\n"
                      "ya message mein packages likho (jaise: qrcode pillow).", back_kb(n))

    elif act == "del":
        await show(u, f"⚠️ bot[{n}] delete karna hai?",
                   Kb([[Btn("✅ Haan, delete", callback_data=f"delok:{n}"),
                        Btn("❌ Nahi", callback_data=f"b:{n}")]]))

    elif act == "delok":
        stop(n); reg["bots"].pop(str(n), None)
        for p in (pyp(n), logp(n), reqp(n)):
            if os.path.exists(p): os.remove(p)
        await save(c.bot)
        await show(u, f"🗑 bot[{n}] delete ho gaya.", Kb([[Btn("🤖 My bots", callback_data="list")]]))


async def apply_req(u, c, n, text, kind):
    msg = await u.message.reply_text("⏳ Requirements install ho rahi hain…")
    ok, out = await asyncio.to_thread(pip_install, n, text)
    if not ok:
        c.user_data["state"] = (kind, n)
        return await msg.edit_text(
            f"❌ Install fail:\n{out}\n\nDobara bhejo ya cancel karo.",
            reply_markup=Kb([[Btn("❌ Cancel", callback_data=f"b:{n}")]]))
    reg["bots"][str(n)]["req"] = text
    if kind == "req_new" or reg["bots"][str(n)].get("run"):
        run(n); reg["bots"][str(n)]["run"] = True
    await save(c.bot)
    await msg.edit_text(f"✅ Requirements install ho gayi.\nbot[{n}] {'🟢 chal raha hai' if alive(n) else 'ready'}.",
                        reply_markup=back_kb(n))

async def on_doc(u, c):
    if u.effective_user.id != OWNER: return
    st = c.user_data.get("state")
    if not st:
        return await u.message.reply_text("Pehle menu se option chuno 👇", reply_markup=main_kb())
    d = u.message.document
    kind = st[0]

    if kind in ("py_new", "py_upd"):
        if not d.file_name.endswith(".py"):
            return await u.message.reply_text("❌ Sirf .py file bhejo.")
        if kind == "py_new":
            n = reg["next"]; reg["next"] += 1
        else:
            n = st[1]
        f = await c.bot.get_file(d.file_id)
        await f.download_to_drive(pyp(n))
        if kind == "py_new":
            reg["bots"][str(n)] = {"name": d.file_name, "file_id": d.file_id, "run": False, "req": ""}
            await save(c.bot)
            c.user_data["state"] = ("req_new", n)
            return await u.message.reply_text(
                f"✅ File mil gayi (bot[{n}]).\n\n📦 Ab requirements.txt bhejo, ya packages likho "
                "(jaise: qrcode pillow).\nNahi chahiye to Skip dabao.",
                reply_markup=Kb([[Btn("⏭ Skip", callback_data="skip")]]))
        reg["bots"][str(n)]["file_id"] = d.file_id
        reg["bots"][str(n)]["name"] = d.file_name
        c.user_data.pop("state", None)
        if reg["bots"][str(n)].get("run"):
            run(n)
        await save(c.bot)
        return await u.message.reply_text(f"✅ bot[{n}] ki file update ho gayi"
                                          f"{' aur restart ho gaya' if alive(n) else ''}.",
                                          reply_markup=back_kb(n))

    if kind in ("req_new", "req_upd"):
        if not d.file_name.endswith(".txt"):
            return await u.message.reply_text("❌ requirements .txt file honi chahiye.")
        f = await c.bot.get_file(d.file_id)
        tmp = f"{DIR}/_tmp_req.txt"
        await f.download_to_drive(tmp)
        text = open(tmp, errors="ignore").read()
        await apply_req(u, c, st[1], text, kind)

async def on_text(u, c):
    if u.effective_user.id != OWNER: return
    st = c.user_data.get("state")
    if st and st[0] in ("req_new", "req_upd"):
        pkgs = [p for p in re.split(r"[,\s]+", u.message.text.strip()) if p]
        return await apply_req(u, c, st[1], "\n".join(pkgs), st[0])
    await u.message.reply_text("Menu 👇", reply_markup=main_kb())


# ---------------- startup / watchdog ----------------
async def watchdog():
    while True:
        await asyncio.sleep(30)
        for i, b in list(reg["bots"].items()):
            if b.get("run") and not alive(int(i)):
                run(int(i))

async def post_init(app):
    global reg, reg_msg_id
    chat = await app.bot.get_chat(OWNER)
    pm = chat.pinned_message
    if pm and pm.text and pm.text.startswith("REGISTRY\n"):
        reg = json.loads(pm.text.split("\n", 1)[1])
        reg_msg_id = pm.message_id
        for i, b in reg["bots"].items():
            f = await app.bot.get_file(b["file_id"])
            await f.download_to_drive(pyp(i))
            if b.get("req"):
                await asyncio.to_thread(pip_install, i, b["req"])
            if b.get("run"):
                run(int(i))
    asyncio.create_task(watchdog())


def main():
    threading.Thread(target=web, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "menu"], menu))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()

if __name__ == "__main__":
    main()
