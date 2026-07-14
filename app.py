import uuid

from flask import Flask, redirect, render_template, request, url_for

from extensions import socketio


app = Flask(__name__)
app.secret_key = "shmamale_secret"
socketio.init_app(app)


@app.route("/")
def home():
    return redirect(url_for("homepage"))


@app.route("/homepage")
def homepage():
    return render_template("homepage.html")


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


def bot_options():
    room_id = str(uuid.uuid4())[:4].upper()
    try:
        bot_count = max(1, min(5, int(request.form.get("bot_count", 2))))
    except (TypeError, ValueError):
        bot_count = 2
    bot_difficulty = request.form.get("difficulty", "medium").lower()
    if bot_difficulty not in {"easy", "medium", "hard"}:
        bot_difficulty = "medium"
    return room_id, bot_count, bot_difficulty


@app.route("/botgamescreen", methods=["GET", "POST"])
def bot_game():
    room_id, bot_count, bot_difficulty = bot_options()
    return render_template(
        "botgamescreen.html",
        room_id=room_id,
        bot_count=bot_count,
        bot_difficulty=bot_difficulty,
    )


@app.route("/bot-room", methods=["POST"])
def bot_room_post():
    return bot_game()


@app.route("/multiplayer/<room_id>")
def multiplayer_game(room_id):
    return render_template(
        "game.html",
        room_id=room_id.upper(),
    )


import multiplayer  # noqa: E402,F401


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
