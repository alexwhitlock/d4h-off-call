from flask import Flask, jsonify, render_template, request
import requests
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

API_KEY          = os.environ["D4H_API_KEY"]
TEAM_ID          = os.environ["D4H_TEAM_ID"]
DEFAULT_GROUP_ID = os.environ.get("D4H_GROUP_ID", "")
BASE_URL         = "https://api.team-manager.ca.d4h.com/v3"


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


def _fetch_off_duties(member_ids):
    today = datetime.now(timezone.utc).date().isoformat()
    after = f"{today}T00:00:00Z"
    member_params = "&".join(f"member_id={mid}" for mid in member_ids)

    duties = []
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
        duties.extend(batch)
        page += 1

    return [d for d in duties if d.get("type") == "OFF"]


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


def get_data(group_id):
    members = _fetch_group_members(group_id)
    member_ids = [m["id"] for m in members]

    if not member_ids:
        return {
            "generated": datetime.now(timezone.utc).isoformat(),
            "group_size": 0,
            "members": [],
            "entries": []
        }

    raw_entries = _fetch_off_duties(member_ids)

    # Back-fill names from duty entries for any members missing names
    name_map = {}
    for e in raw_entries:
        mid  = str((e.get("member") or {}).get("id", ""))
        name = (e.get("member") or {}).get("name", "")
        if mid and name:
            name_map[mid] = name

    for m in members:
        if not m["name"] and m["id"] in name_map:
            m["name"] = name_map[m["id"]]

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "group_size": len(members),
        "members": members,
        "entries": [
            _strip_entry(e)
            for e in sorted(raw_entries, key=lambda d: d.get("startsAt") or "")
        ],
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/groups")
def api_groups():
    try:
        groups = _fetch_groups()
        return jsonify({"groups": groups, "default_group_id": DEFAULT_GROUP_ID})
    except requests.HTTPError as e:
        return jsonify({"error": f"D4H API error: {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/data")
def api_data():
    group_id = request.args.get("group_id", DEFAULT_GROUP_ID)
    if not group_id:
        return jsonify({"error": "No group selected"}), 400
    try:
        return jsonify(get_data(group_id))
    except requests.HTTPError as e:
        return jsonify({"error": f"D4H API error: {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
