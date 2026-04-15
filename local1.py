from flask import Flask, request, Response
from index import handler, handle_events

app = Flask(__name__)

# Slash command
@app.route("/slack", methods=["POST"])
def slack_command():
    return handler(request)

# Events (thread replies)
@app.route("/slack/events", methods=["POST"])
def slack_events():
    return handle_events(request)

if __name__ == "__main__":
    app.run(port=3000)