import os
import time
import threading
import subprocess
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ── Config from env vars ──────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
CHAT_ID     = os.environ["CHAT_ID"]
SECRET_KEY  = os.environ.get("SECRET_KEY", "changeme123")  # shared with PC client

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    "pc_online":        False,
    "tracking":         False,
    "last_heartbeat":   None,   # epoch float
    "track_msg_id":     None,   # telegram message id to edit
    "startup_msg_id":   None,
    "pending_cmd":      None,   # "shutdown" | "locate" | None
    "locate_result":    None,   # string from client
}
state_lock = threading.Lock()

# ── Telegram helpers ──────────────────────────────────────────────────────────

def tg(method, **kwargs):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[TG ERROR] {method}: {e}")
        return {}

def send(text, parse_mode="HTML"):
    res = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode=parse_mode)
    return res.get("result", {}).get("message_id")

def edit(msg_id, text, parse_mode="HTML"):
    tg("editMessageText", chat_id=CHAT_ID, message_id=msg_id,
       text=text, parse_mode=parse_mode)

def delete_msg(msg_id):
    tg("deleteMessage", chat_id=CHAT_ID, message_id=msg_id)

# ── Tracking loop ─────────────────────────────────────────────────────────────

def tracking_loop():
    """Runs in a background thread. Every 60 s updates the 'last seen' message."""
    while True:
        time.sleep(60)
        with state_lock:
            if not state["tracking"] or not state["track_msg_id"]:
                continue
            if state["last_heartbeat"] is None:
                continue
            elapsed = int((time.time() - state["last_heartbeat"]) / 60)
            # if PC went offline (no heartbeat for > 3 min), say so
            if elapsed > 3:
                state["pc_online"] = False
                state["tracking"]  = False
                mid = state["track_msg_id"]
                state["track_msg_id"] = None
            else:
                mid = state["track_msg_id"]

        if elapsed > 3:
            edit(mid, "🔴 <b>PC offline</b> — nessun segnale negli ultimi 3 minuti.")
        else:
            label = "1 minuto fa" if elapsed <= 1 else f"{elapsed} minuti fa"
            edit(mid, f"🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>{label}</b>")

threading.Thread(target=tracking_loop, daemon=True).start()

# ── Telegram webhook ──────────────────────────────────────────────────────────

def handle_update(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = str(msg.get("chat", {}).get("id", ""))
    if chat != str(CHAT_ID):
        return  # ignore strangers

    text = msg.get("text", "").strip()

    if text == "/track":
        with state_lock:
            if not state["pc_online"]:
                send("⚠️ Il PC è offline, impossibile avviare il tracking.")
                return
            if state["tracking"]:
                send("ℹ️ Tracking già attivo.")
                return
            state["tracking"] = True
            mid = send("🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>adesso</b>")
            state["track_msg_id"] = mid

    elif text == "/shutdown":
        with state_lock:
            if not state["pc_online"]:
                send("⚠️ Il PC è già offline.")
                return
            state["pending_cmd"] = "shutdown"
        send("⚡ Comando di spegnimento inviato.")

    elif text == "/locate":
        with state_lock:
            if not state["pc_online"]:
                send("⚠️ Il PC è offline.")
                return
            state["pending_cmd"] = "locate"
            state["locate_result"] = None
        send("🔍 Richiesta app attive inviata, attendo risposta...")

    elif text == "/status":
        with state_lock:
            online = state["pc_online"]
            tracking = state["tracking"]
            hb = state["last_heartbeat"]
        elapsed = int((time.time() - hb) / 60) if hb else None
        status = "🟢 Online" if online else "🔴 Offline"
        tr = "✅ Attivo" if tracking else "❌ Disattivo"
        hb_str = (f"{elapsed} min fa" if elapsed is not None else "N/D")
        send(f"<b>Stato PC</b>\n{status}\nTracking: {tr}\nUltimo segnale: {hb_str}")

    elif text == "/help":
        send(
            "<b>Comandi disponibili</b>\n\n"
            "/status — Stato attuale del PC\n"
            "/track — Avvia tracking manuale\n"
            "/locate — App aperte negli ultimi 5 min\n"
            "/shutdown — Spegni il PC\n"
            "/help — Questo messaggio"
        )

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
    data = request.json or {}
    auto_track = data.get("auto_track", False)

    with state_lock:
        state["pc_online"]      = True
        state["last_heartbeat"] = time.time()
        state["pending_cmd"]    = None

    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    mid = send(f"🖥️ <b>PC acceso</b> — {now}")

    with state_lock:
        state["startup_msg_id"] = mid
        if auto_track:
            state["tracking"] = True
            track_mid = send("🟢 <b>PC online</b>\n🕐 Visto l'ultima volta: <b>adesso</b>")
            state["track_msg_id"] = track_mid

    return jsonify({"ok": True})

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    if not verify(request):
        return jsonify({"error": "forbidden"}), 403

    with state_lock:
        state["pc_online"]      = True
        state["last_heartbeat"] = time.time()
        cmd = state["pending_cmd"]
        if cmd:
            state["pending_cmd"] = None  # consuma il comando, non ripetere

    return jsonify({"cmd": cmd})

@app.route("/cmd_done", methods=["POST"])
def cmd_done():
    if not verify(request):
        return jsonify({"error": "forbidden"}), 403
    data = request.json or {}
    cmd  = data.get("cmd")

    with state_lock:
        state["pending_cmd"] = None
        if cmd == "locate":
            apps = data.get("result", "Nessuna app trovata.")
            state["locate_result"] = apps

    if cmd == "locate":
        send(f"📋 <b>App attive (ultimi 5 min)</b>:\n<code>{apps}</code>")
    elif cmd == "shutdown":
        with state_lock:
            state["pc_online"] = False
            state["tracking"]  = False
            mid = state["track_msg_id"]
            state["track_msg_id"] = None
        if mid:
            edit(mid, "🔴 <b>PC offline</b> — spento da remoto.")

    return jsonify({"ok": True})

# ── Webhook setup & keepalive ─────────────────────────────────────────────────

@app.route("/set_webhook")
def set_webhook():
    url = request.args.get("url")
    if not url:
        return "Passa ?url=https://tuo-server.render.com/webhook"
    res = tg("setWebhook", url=url)
    return jsonify(res)

@app.route("/ping")
def ping():
    return "pong"

@app.route("/")
def index():
    return "ok"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
