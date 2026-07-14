from flask_socketio import SocketIO

# Single shared SocketIO instance. It lives in its own module (instead of app.py)
# so that `python app.py` and `from app import socketio` can never create two
# competing instances — event handlers always register on the one that runs.
socketio = SocketIO(manage_session=True, cors_allowed_origins="*", async_mode="threading")
