import asyncio, json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_ID"])
DIR = "hosted"
os.makedirs(DIR, exist_ok=True)

# Registry survives Render restarts: it is stored in a pinned message in your chat with the bot.
reg = {"next": 1, "bots": {}, "pip": []}
reg_msg_id = None
procs = {}


# ---------- tiny web server so Render sees an open port ----------
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"alive")
    def log_message(self, *a): pass

def web():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Ping).serve_forever()


# ---------- helpers ----------
def path(n): return f"{DIR}/bot{n}.py"
def logp(n): return f"{DIR}/bot{n}.log"

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
    except Exception as e:
        if "not modified" not in str(e):
            print("save error:", e)

def run(n):
    stop(n)
    out = open(logp(n), "ab")
    procs[n] = subprocess.Popen([sys.executable, "-u", path(n)], stdout=out, stderr=subprocess.STDOUT)

def stop(n):
    p = procs.pop(n, None)
    if p and p.poll() is None:
        p.terminate()
        try: p.wait(5)
        except Exception: p.kill()

def pip_install(pkgs):
    r = subprocess.run([sys.executable, "-m", "pip", "install", *pkgs], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr)[-800:]

def status(n):
    p = procs.get(n)
    return "🟢 running" if p and p.poll() is None else "🔴 stopped"

def owner_only(fn):
    async def w(u: Update, c: ContextTypes.DEFAULT_TYPE):
        if u.effective_user.id != OWNER: return
        return await fn(u, c)
    return w

def arg_n(c):
    try:
        n = int(c.args[0].lower().replace("bot", "").strip("[]"))
        return n if str(n) in reg["bots"] else None
    except Exception:
        return None


# ---------- commands ----------
@owner_only
async def start_cmd(u, c):
    if c.args:
        return await start_bot(u, c)
    await u.message.reply_text(
        "Send me a .py file and I'll host it as bot[N].\n\n"
        "/list – all bots\n/stop N · /start N · /restart N\n/logs N – last output\n"
        "/delete N\n/pip pkg1 pkg2 – install libraries for your bots")

@owner_only
async def list_cmd(u, c):
    if not reg["bots"]:
        return await u.message.reply_text("No bots yet. Upload a .py file.")
    lines = [f"bot[{n}] {b['name']} – {status(int(n))}" for n, b in reg["bots"].items()]
    await u.message.reply_text("\n".join(lines) + f"\n\npip: {' '.join(reg['pip']) or '-'}")

@owner_only
async def upload(u, c):
    d = u.message.document
    if not d.file_name.endswith(".py"):
        return await u.message.reply_text("Only .py files please.")
    n = reg["next"]; reg["next"] += 1
    f = await c.bot.get_file(d.file_id)
    await f.download_to_drive(path(n))
    reg["bots"][str(n)] = {"name": d.file_name, "file_id": d.file_id, "run": True}
    run(n)
    await save(c.bot)
    await u.message.reply_text(f"✅ Hosted as bot[{n}]. Check /logs {n}")

@owner_only
async def stop_cmd(u, c):
    n = arg_n(c)
    if not n: return await u.message.reply_text("Usage: /stop 1")
    stop(n); reg["bots"][str(n)]["run"] = False; await save(c.bot)
    await u.message.reply_text(f"bot[{n}] stopped")

@owner_only
async def start_bot(u, c):
    n = arg_n(c)
    if not n: return await u.message.reply_text("Usage: /start 1")
    run(n); reg["bots"][str(n)]["run"] = True; await save(c.bot)
    await u.message.reply_text(f"bot[{n}] started")

@owner_only
async def logs_cmd(u, c):
    n = arg_n(c)
    if not n: return await u.message.reply_text("Usage: /logs 1")
    try: t = open(logp(n), errors="ignore").read()[-3000:] or "(empty)"
    except FileNotFoundError: t = "(no logs)"
    await u.message.reply_text(f"bot[{n}] {status(n)}\n\n{t}")

@owner_only
async def delete_cmd(u, c):
    n = arg_n(c)
    if not n: return await u.message.reply_text("Usage: /delete 1")
    stop(n); reg["bots"].pop(str(n))
    for p in (path(n), logp(n)):
        if os.path.exists(p): os.remove(p)
    await save(c.bot)
    await u.message.reply_text(f"bot[{n}] deleted")

@owner_only
async def pip_cmd(u, c):
    if not c.args: return await u.message.reply_text("Usage: /pip requests aiogram")
    await u.message.reply_text("Installing…")
    ok, out = await asyncio.to_thread(pip_install, c.args)
    if ok:
        reg["pip"] = sorted(set(reg["pip"]) | set(c.args)); await save(c.bot)
    await u.message.reply_text(("✅ Done" if ok else "❌ Failed") + "\n" + out[-500:])


# ---------- startup: restore everything after a Render restart ----------
async def watchdog(bot):
    while True:
        await asyncio.sleep(30)
        for n, b in reg["bots"].items():
            p = procs.get(int(n))
            if b.get("run") and (p is None or p.poll() is not None):
                run(int(n))

async def post_init(app):
    global reg_msg_id, reg
    chat = await app.bot.get_chat(OWNER)
    pm = chat.pinned_message
    if pm and pm.text and pm.text.startswith("REGISTRY\n"):
        reg = json.loads(pm.text.split("\n", 1)[1]); reg_msg_id = pm.message_id
        if reg["pip"]:
            await asyncio.to_thread(pip_install, reg["pip"])
        for n, b in reg["bots"].items():
            f = await app.bot.get_file(b["file_id"])
            await f.download_to_drive(path(n))
            if b.get("run"): run(int(n))
    asyncio.create_task(watchdog(app.bot))


def main():
    threading.Thread(target=web, daemon=True).start()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("startbot", start_bot))
    app.add_handler(CommandHandler("restart", start_bot))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("pip", pip_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, upload))
    app.run_polling()

if __name__ == "__main__":
    main()
