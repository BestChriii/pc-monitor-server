import os
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify, g
import requests

app = Flask(__name__)

BOT_TOKEN  = os.environ["BOT_TOKEN"]
CHAT_ID    = os.environ["CHAT_ID"]
SECRET_KEY = os.environ.get("SECRET_KEY", "changeme123")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "pc_online":      False,
    "tracking":       False,
    "last_heartbeat": None,
    "track_msg_id":   None,
    "pending_cmd":    None,   # dict {cmd, args} oppure None
    "confirm_pending": None,  # {cmd, msg_id, expires}
}
lock = threading.Lock()

# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[TG] {method} error: {e}")
        return {}

def send(text, parse_mode="HTML", reply_markup=None):
    kwargs = dict(chat_id=CHAT_ID, text=text, parse_mode=parse_mode)
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    res = tg("sendMessage", **kwargs)
    return res.get("result", {}).get("message_id")

def edit(msg_id, text, parse_mode="HTML", reply_markup=None):
    kwargs = dict(chat_id=CHAT_ID, message_id=msg_id, text=text, parse_mode=parse_mode)
    if reply_markup:
        kwargs["reply_markup"] = reply_markup
    tg("editMessageText", **kwargs)

def delete_msg(msg_id):
    tg("deleteMessage", chat_id=CHAT_ID, message_id=msg_id)

def send_photo(photo_url=None, photo_data=None, caption=""):
    if photo_url:
        tg("sendPhoto", chat_id=CHAT_ID, photo=photo_url, caption=caption)

def confirm_keyboard(cmd_key):
    return {
        "inline_keyboard": [[
            {"text": "✅ Sì", "callback_data": f"confirm:{cmd_key}"},
            {"text": "❌ No", "callback_data": f"cancel:{cmd_key}"}
        ]]
    }

# ── Tracking loop ─────────────────────────────────────────────────────────────

def tracking_loop():
    while True:
        time.sleep(60)
        with lock:
            if not state["tracking"] or not state["track_msg_id"]:
                continue
            hb = state["last_heartbeat"]
            mid = state["track_msg_id"]
            if hb is None:
                continue
            elapsed = int((time.time() - hb) / 60)
            went_offline = elapsed > 3
            if went_offline:
                state["pc_online"] = False
                state["tracking"]  = False
                state["track_msg_id"] = None

        if went_offline:
            edit(mid, "🔴 <b>PC offline</b>\nNessun segnale da più di 3 minuti.")
        else:
            label = "adesso" if elapsed == 0 else ("1 minuto fa" if elapsed == 1 else f"{elapsed} minuti fa")
            edit(mid, f"🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>{label}</b>")

threading.Thread(target=tracking_loop, daemon=True).start()

# ── Confirm timeout loop ──────────────────────────────────────────────────────

def confirm_timeout_loop():
    while True:
        time.sleep(5)
        with lock:
            cp = state["confirm_pending"]
            if cp and time.time() > cp["expires"]:
                mid = cp["msg_id"]
                state["confirm_pending"] = None
            else:
                mid = None
        if mid:
            edit(mid, "⏰ Conferma scaduta — comando annullato.")

threading.Thread(target=confirm_timeout_loop, daemon=True).start()

# ── Command helpers ───────────────────────────────────────────────────────────

def require_online():
    if not state["pc_online"]:
        send("⚠️ Il PC è offline.")
        return False
    return True

def set_pending(cmd, args=None):
    state["pending_cmd"] = {"cmd": cmd, "args": args or ""}

def ask_confirm(cmd_key, label, args=None):
    """Invia messaggio con bottoni Sì/No e salva in confirm_pending."""
    kbd = confirm_keyboard(cmd_key)
    mid = send(f"⚠️ <b>Conferma richiesta</b>\nVuoi davvero eseguire: <b>{label}</b>?", reply_markup=kbd)
    state["confirm_pending"] = {
        "cmd":     cmd_key,
        "args":    args or "",
        "msg_id":  mid,
        "expires": time.time() + 30
    }

# ── Telegram webhook handler ──────────────────────────────────────────────────

def handle_update(update):
    # Callback da bottoni inline
    if "callback_query" in update:
        cq   = update["callback_query"]
        data = cq.get("data", "")
        cq_id = cq["id"]
        msg_id = cq["message"]["message_id"]

        tg("answerCallbackQuery", callback_query_id=cq_id)

        action, _, cmd_key = data.partition(":")

        with lock:
            cp = state["confirm_pending"]
            if cp is None or cp["cmd"] != cmd_key:
                edit(msg_id, "ℹ️ Questa conferma non è più valida.")
                return
            args = cp.get("args", "")
            state["confirm_pending"] = None

        if action == "cancel":
            edit(msg_id, f"❌ Comando <b>{cmd_key}</b> annullato.")
            return

        # Confermato — esegui
        edit(msg_id, f"✅ Esecuzione <b>{cmd_key}</b> confermata...")
        with lock:
            _dispatch_confirmed(cmd_key, args)
        return

    # Messaggio testuale
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID):
        return

    text = msg.get("text", "").strip()
    if not text:
        return

    parts    = text.split(None, 1)
    cmd      = parts[0].lower()
    args     = parts[1] if len(parts) > 1 else ""

    with lock:
        online = state["pc_online"]

    # ── Comandi che non richiedono PC online ──
    if cmd == "/help":
        send(
            "<b>📋 Comandi disponibili</b>\n\n"
            "<b>Info</b>\n"
            "/status — Stato PC e tracking\n"
            "/componenti — CPU, RAM, GPU, disco\n"
            "/ping — Latenza del PC\n\n"
            "<b>Controllo</b>\n"
            "/shutdown — Spegni il PC\n"
            "/reboot — Riavvia il PC\n"
            "/logout — Disconnetti l'utente\n"
            "/lock — Blocca lo schermo\n\n"
            "<b>App</b>\n"
            "/locate — App attive (ultimi 5 min)\n"
            "/open [nome] — Apri un'applicazione\n"
            "/close [nome] — Chiudi un'applicazione\n\n"
            "<b>Media</b>\n"
            "/screenshot — Cattura schermo\n"
            "/rec-start — Avvia registrazione\n"
            "/rec-stop — Ferma e invia registrazione\n\n"
            "<b>Tracking</b>\n"
            "/track — Attiva tracking manuale\n"
            "/stoptrack — Disattiva tracking"
        )
        return

    if cmd == "/status":
        with lock:
            online   = state["pc_online"]
            tracking = state["tracking"]
            hb       = state["last_heartbeat"]
        elapsed = int((time.time() - hb) / 60) if hb else None
        hb_str  = f"{elapsed} min fa" if elapsed is not None else "N/D"
        status  = "🟢 Online" if online else "🔴 Offline"
        tr      = "✅ Attivo" if tracking else "❌ Disattivo"
        send(
            f"<b>🖥️ Stato PC</b>\n\n"
            f"Connessione: {status}\n"
            f"Tracking: {tr}\n"
            f"Ultimo segnale: {hb_str}"
        )
        return

    # ── Comandi che richiedono PC online ──
    if not online:
        send("⚠️ Il PC è offline, comando non disponibile.")
        return

    with lock:
        if cmd == "/shutdown":
            ask_confirm("shutdown", "Spegni il PC")

        elif cmd == "/reboot":
            ask_confirm("reboot", "Riavvia il PC")

        elif cmd == "/logout":
            ask_confirm("logout", "Disconnetti l'utente")

        elif cmd == "/rec-stop":
            ask_confirm("rec-stop", "Ferma registrazione e invia video")

        elif cmd == "/lock":
            set_pending("lock")
            send("🔒 Blocco schermo inviato.")

        elif cmd == "/locate":
            set_pending("locate")
            send("🔍 Recupero app attive...")

        elif cmd == "/screenshot":
            set_pending("screenshot")
            send("📸 Screenshot in corso...")

        elif cmd == "/rec-start":
            set_pending("rec-start")
            send("🔴 Avvio registrazione...")

        elif cmd == "/componenti":
            set_pending("componenti")
            send("🔧 Recupero info hardware...")

        elif cmd == "/ping":
            set_pending("ping")
            send("📡 Ping in corso...")

        elif cmd == "/track":
            if state["tracking"]:
                send("ℹ️ Tracking già attivo.")
            else:
                state["tracking"] = True
                mid = send("🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>adesso</b>")
                state["track_msg_id"] = mid

        elif cmd == "/stoptrack":
            if not state["tracking"]:
                send("ℹ️ Tracking non attivo.")
            else:
                state["tracking"] = False
                mid = state["track_msg_id"]
                state["track_msg_id"] = None
                if mid:
                    edit(mid, "⏹️ Tracking disattivato.")
                send("✅ Tracking fermato.")

        elif cmd == "/open":
            if not args:
                send("⚠️ Uso: /open [nome applicazione]")
            else:
                set_pending("open-search", args)
                send(f"🔍 Cerco <b>{args}</b> nel menu Start...")

        elif cmd == "/close":
            if not args:
                send("⚠️ Uso: /close [nome applicazione]\nSe il nome non è valido riceverai la lista dei processi aperti.")
            else:
                set_pending("close", args)
                send(f"❌ Chiusura <b>{args}</b>...")

        else:
            send(f"❓ Comando sconosciuto: <code>{cmd}</code>\nUsa /help per la lista.")

def _dispatch_confirmed(cmd_key, args):
    """Chiamato dentro lock dopo conferma bottone."""
    if cmd_key == "shutdown":
        set_pending("shutdown")
    elif cmd_key == "reboot":
        set_pending("reboot")
    elif cmd_key == "logout":
        set_pending("logout")
    elif cmd_key == "rec-stop":
        set_pending("rec-stop")
    elif cmd_key == "open-exec":
        set_pending("open-exec", args)

@app.route("/webhook", methods=["POST"])
def webhook():
    handle_update(request.json or {})
    return "ok"

# ── PC client endpoints ───────────────────────────────────────────────────────

def verify(req):
    return req.headers.get("X-Secret") == SECRET_KEY

@app.route("/startup", methods=["POST"])
def startup():
    if not verify(request):
        return jsonify({"error": "forbidden"}), 403
    data       = request.json or {}
    auto_track = data.get("auto_track", False)

    with lock:
        state["pc_online"]      = True
        state["last_heartbeat"] = time.time()
        state["pending_cmd"]    = None

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    send(f"🖥️ <b>PC acceso</b>\n🕐 {now}")

    if auto_track:
        with lock:
            state["tracking"] = True
            mid = send("🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>adesso</b>")
            state["track_msg_id"] = mid

    return jsonify({"ok": True})

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    if not verify(request):
        return jsonify({"error": "forbidden"}), 403

    with lock:
        state["pc_online"]      = True
        state["last_heartbeat"] = time.time()
        cmd_obj = state["pending_cmd"]
        if cmd_obj:
            state["pending_cmd"] = None

    return jsonify({"cmd": cmd_obj["cmd"] if cmd_obj else None,
                    "args": cmd_obj["args"] if cmd_obj else ""})

@app.route("/cmd_done", methods=["POST"])
def cmd_done():
    if not verify(request):
        return jsonify({"error": "forbidden"}), 403
    data   = request.json or {}
    cmd    = data.get("cmd", "")
    result = data.get("result", "")
    ok     = data.get("ok", True)

    if cmd == "locate":
        send(f"📋 <b>App attive (ultimi 5 min)</b>\n\n{result}")

    elif cmd == "screenshot":
        # result è base64 jpeg — inviamo come foto
        if result:
            import base64
            img_bytes = base64.b64decode(result)
            try:
                requests.post(
                    f"{TELEGRAM_API}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": "📸 Screenshot"},
                    files={"photo": ("screen.jpg", img_bytes, "image/jpeg")},
                    timeout=30
                )
            except Exception as e:
                send(f"⚠️ Errore invio screenshot: {e}")

    elif cmd == "componenti":
        send(f"🔧 <b>Componenti PC</b>\n\n{result}")

    elif cmd == "ping":
        send(f"📡 <b>Ping</b>\n{result}")

    elif cmd == "rec-stop":
        # result è un link o un path
        if result:
            send(f"🎬 <b>Registrazione completata</b>\n📥 Link: {result}")
        else:
            send("🎬 Registrazione fermata. Nessun file disponibile.")

    elif cmd == "open-search":
        if ok:
            # result = "Nome Reale|path\completo"
            parts = result.split("|", 1)
            display_name = parts[0] if parts else result
            exec_path    = parts[1] if len(parts) > 1 else result
            with lock:
                kbd = confirm_keyboard("open-exec")
                mid = send(
                    f"🚀 <b>Trovato:</b> <code>{display_name}</code>\nVuoi veramente avviarlo?",
                    reply_markup=kbd
                )
                state["confirm_pending"] = {
                    "cmd":     "open-exec",
                    "args":    exec_path,
                    "msg_id":  mid,
                    "expires": time.time() + 30
                }
        else:
            send(f"\u26a0\ufe0f Nessuna app trovata per <b>{result}</b>.\nControlla il nome e riprova.")


    elif cmd == "open":
        if ok:
            send("✅ Applicazione avviata.")
        else:
            send("⚠️ Impossibile aprire l'applicazione.")

    elif cmd == "close":
        if ok:
            send(f"✅ Applicazione chiusa.")
        else:
            # result contiene la lista processi
            send(f"⚠️ App non trovata. <b>Processi aperti:</b>\n\n{result}")

    elif cmd in ("shutdown", "reboot", "logout"):
        with lock:
            state["pc_online"] = False
            state["tracking"]  = False
            mid = state["track_msg_id"]
            state["track_msg_id"] = None
        labels = {"shutdown": "spento", "reboot": "riavviato", "logout": "disconnesso"}
        if mid:
            edit(mid, f"🔴 <b>PC offline</b> — {labels.get(cmd, cmd)} da remoto.")

    elif cmd == "lock":
        send("🔒 Schermo bloccato.")

    elif cmd == "rec-start":
        if ok:
            send("🔴 <b>Registrazione avviata.</b>\nUsa /rec-stop per fermarla.")
        else:
            send("⚠️ Impossibile avviare la registrazione.")

    return jsonify({"ok": True})

# ── Misc ──────────────────────────────────────────────────────────────────────

@app.route("/set_webhook")
def set_webhook():
    url = request.args.get("url")
    if not url:
        return "Passa ?url=https://tuo-server.onrender.com/webhook"
    return jsonify(tg("setWebhook", url=url))

@app.route("/ping")
def ping():
    return f"pong - {(time.perf_counter() - request_start)*1000:.2f} ms"
    
@app.route("/")
def index():
    return "applicazione online"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
