from flask import Flask, jsonify, render_template, request, Response, stream_with_context
import requests
import os
import json
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY          = os.environ["D4H_API_KEY"]
TEAM_ID          = os.environ["D4H_TEAM_ID"]
DEFAULT_GROUP_ID = os.environ.get("D4H_GROUP_ID", "")
BASE_URL         = "https://api.team-manager.ca.d4h.com/v3"
DATA_DIR         = Path(os.environ.get("DATA_DIR", "data"))
FAVOURITES_FILE  = DATA_DIR / "favourites.json"


def _read_favourites():
    try:
        return json.loads(FAVOURITES_FILE.read_text())
    except Exception:
        return []


def _write_favourites(ids):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FAVOURITES_FILE.write_text(json.dumps(ids))


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def _fetch_groups():
    url = f"{BASE_URL}/team/{TEAM_ID}/member-groups?size=200"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    groups = []
    for g in results:
        name = g.get("title") or g.get("name") or g.get("label") or f"Group {g.get('id')}"
        groups.append({"id": str(g["id"]), "name": name})
    return sorted(groups, key=lambda g: g["name"].lower())


def _fetch_group_members(group_id):
    url = f"{BASE_URL}/team/{TEAM_ID}/member-group-memberships?group_id={group_id}&size=200"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    members = []
    for m in results:
        if "member" in m:
            mem = m["member"]
            members.append({
                "id": str(mem["id"]),
                "name": mem.get("name", "")
            })
    return members


def _fetch_member_names(member_ids):
    names = {}
    for mid in member_ids:
        try:
            url = f"{BASE_URL}/team/{TEAM_ID}/members/{mid}"
            resp = requests.get(url, headers=_headers(), timeout=10)
            if not resp.ok:
                continue
            name = resp.json().get("name", "")
            if name:
                names[mid] = name
        except Exception:
            pass
    return names


def _strip_entry(entry):
    member = entry.get("member") or {}
    return {
        "id": entry.get("id"),
        "type": entry.get("type"),
        "startsAt": entry.get("startsAt"),
        "endsAt": entry.get("endsAt"),
        "notes": entry.get("notes", ""),
        "member": {
            "id": str(member.get("id", "")),
            "name": member.get("name", "")
        },
    }


def _evt(data):
    return f"data: {json.dumps(data)}\n\n"


def _stream_data(group_id):
    """Generator that yields SSE events for a group data load."""
    try:
        yield _evt({"status": "Fetching group members…"})

        members = _fetch_group_members(group_id)
        member_ids = [m["id"] for m in members]

        if not member_ids:
            yield _evt({"done": True, "result": {
                "generated": datetime.now(timezone.utc).isoformat(),
                "group_size": 0, "members": [], "entries": []
            }})
            return

        yield _evt({"status": f"Fetching off-call entries… ({len(members)} members)"})

        today = datetime.now(timezone.utc).date().isoformat()
        after = f"{today}T00:00:00Z"
        member_params = "&".join(f"member_id={mid}" for mid in member_ids)

        all_duties = []
        page = 0
        while True:
            url = (
                f"{BASE_URL}/team/{TEAM_ID}/duties"
                f"?after={after}&before=2099-12-31T23:59:59Z"
                f"&{member_params}&page={page}&size=100&order=asc"
            )
            resp = requests.get(url, headers=_headers(), timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("results", [])
            if not batch:
                break
            all_duties.extend(batch)
            page += 1
            yield _evt({"status": f"Fetching off-call entries… ({len(all_duties)} found)"})

        raw_entries = [d for d in all_duties if d.get("type") == "OFF"]

        # Back-fill names from duty entries
        name_map = {}
        for e in raw_entries:
            mid  = str((e.get("member") or {}).get("id", ""))
            name = (e.get("member") or {}).get("name", "")
            if mid and name:
                name_map[mid] = name
        for m in members:
            if not m["name"] and m["id"] in name_map:
                m["name"] = name_map[m["id"]]

        still_unnamed = [m["id"] for m in members if not m["name"]]
        if still_unnamed:
            yield _evt({"status": f"Fetching {len(still_unnamed)} member name(s)…"})
            fetched = _fetch_member_names(still_unnamed)
            for m in members:
                if not m["name"] and m["id"] in fetched:
                    m["name"] = fetched[m["id"]]

        result = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "group_size": len(members),
            "members": members,
            "entries": [
                _strip_entry(e)
                for e in sorted(raw_entries, key=lambda d: d.get("startsAt") or "")
            ],
        }
        yield _evt({"done": True, "result": result})

    except requests.HTTPError as e:
        yield _evt({"error": f"D4H API error: {e.response.status_code}"})
    except GeneratorExit:
        pass
    except Exception as e:
        yield _evt({"error": str(e)})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/favourites", methods=["GET"])
def get_favourites():
    return jsonify({"group_ids": _read_favourites()})


@app.route("/api/favourites", methods=["POST"])
def post_favourites():
    ids = (request.get_json() or {}).get("group_ids", [])
    _write_favourites(ids)
    return jsonify({"ok": True})


@app.route("/api/groups")
def api_groups():
    try:
        groups = _fetch_groups()
        return jsonify({"groups": groups, "default_group_id": DEFAULT_GROUP_ID})
    except requests.HTTPError as e:
        return jsonify({"error": f"D4H API error: {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data/stream")
def api_data_stream():
    group_id = request.args.get("group_id", DEFAULT_GROUP_ID)
    if not group_id:
        def _err():
            yield _evt({"error": "No group selected"})
        return Response(stream_with_context(_err()), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return Response(
        stream_with_context(_stream_data(group_id)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/data")
def api_data():
    """Non-streaming fallback (kept for direct API use)."""
    group_id = request.args.get("group_id", DEFAULT_GROUP_ID)
    if not group_id:
        return jsonify({"error": "No group selected"}), 400
    try:
        events = list(_stream_data(group_id))
        for raw in reversed(events):
            msg = json.loads(raw.removeprefix("data: ").strip())
            if "done" in msg:
                return jsonify(msg["result"])
            if "error" in msg:
                return jsonify({"error": msg["error"]}), 502
        return jsonify({"error": "No result produced"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
