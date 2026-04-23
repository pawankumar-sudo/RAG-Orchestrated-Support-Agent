from flask import Flask, request, Response
import json
from index import handle_interactive, handle_events

app = Flask(__name__)

# Button clicks
@app.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    response_data = handle_interactive(request)

    # Safety fallback
    if not response_data:
        return Response("", status=200)

    return Response(
        json.dumps(response_data),
        content_type="application/json",
        status=200
    )

# Events
@app.route("/slack/events", methods=["POST"])
def slack_events():
    return handle_events(request)

if __name__ == "__main__":
    app.run(port=3000)
