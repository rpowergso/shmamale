import os
import secrets
import uuid

from flask import Flask, jsonify, redirect, render_template, request, url_for

from extensions import socketio


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
socketio.init_app(app)


@app.route("/")
def home():
    return redirect(url_for("homepage"))


@app.route("/homepage")
def homepage():
    return render_template("homepage.html")


@app.route("/tutorial")
def tutorial():
    return render_template("tutorial.html")


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/create-room")
def create_room():
    room_id = str(uuid.uuid4())[:4].upper()
    return redirect(url_for("multiplayer_game", room_id=room_id))


@app.route("/join-room", methods=["POST"])
def join_room_post():
    room_id = request.form.get("room_id", "").upper().strip()
    if room_id:
        return redirect(url_for("multiplayer_game", room_id=room_id))
    return redirect(url_for("homepage"))


@app.route("/botgamescreen", methods=["GET", "POST"])
def bot_game():
    return redirect(url_for("create_room"))


@app.route("/bot-room", methods=["POST"])
def bot_room_post():
    return redirect(url_for("create_room"))


@app.route("/multiplayer/<room_id>")
def multiplayer_game(room_id):
    return render_template(
        "game.html",
        room_id=room_id.upper(),
    )


import multiplayer  # noqa: E402,F401


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
