from flask import Flask, jsonify, render_template, request, Response, stream_with_context
import requests
import os
import json
import uuid
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
UNAVAILABLE_FILE = DATA_DIR / "unavailable.json"

GOOGLE_FIELD_ID = 1817
GOOGLE_DOMAIN   = "@sbo-ovsar.ca"

# In-memory cache for /api/me lookups (email → {member, ts})
_me_cache: dict = {}
ME_CACHE_TTL    = 3600


def _read_favourites():
    try:
        return json.loads(FAVOURITES_FILE.read_text())
    except Exception:
        return []


def _write_favourites(ids):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FAVOURITES_FILE.write_text(json.dumps(ids))


def _read_unavailable():
    try:
        return json.loads(UNAVAILABLE_FILE.read_text())
    except Exception:
        return []


def _write_unavailable(entries):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UNAVAILABLE_FILE.write_text(json.dumps(entries, indent=2))


def _headers():
    return {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}


def _cf_email():
    email = request.headers.get("Cf-Access-Authenticated-User-Email", "").strip().lower()
    return email or "local"


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


def _fetch_all_members():
    members, page = [], 0
    while True:
        url = f"{BASE_URL}/team/{TEAM_ID}/members?page={page}&size=100"
        resp = requests.get(url, headers=_headers(), timeout=30)
        resp.raise_for_status()
        body = resp.json()
        batch = body.get("results", [])
        if not batch:
            break
        for m in batch:
            members.append({"id": str(m["id"]), "name": m.get("name", "")})
        page += 1
        if page * 100 >= body.get("totalSize", 0):
            break
    return sorted(members, key=lambda m: (m["name"] or "").lower())


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


def _resolve_me(email):
    """Find the D4H member whose Team Google Account Username field matches email. Cached."""
    now_ts = datetime.now(timezone.utc).timestamp()
    if email in _me_cache and now_ts - _me_cache[email]["ts"] < ME_CACHE_TTL:
        return _me_cache[email]["member"]

    try:
        page = 0
        while True:
            url = f"{BASE_URL}/team/{TEAM_ID}/members?page={page}&size=100"
            resp = requests.get(url, headers=_headers(), timeout=30)
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("results", [])
            if not batch:
                break
            for m in batch:
                # Primary: match custom field 1817 (Team Google Account Username)
                for cfv in m.get("customFieldValues", []):
                    if cfv.get("customField", {}).get("id") == GOOGLE_FIELD_ID:
                        val = (cfv.get("value") or "").strip().lower()
                        if val and (val + GOOGLE_DOMAIN) == email:
                            result = {"id": str(m["id"]), "name": m.get("name", "")}
                            _me_cache[email] = {"member": result, "ts": now_ts}
                            return result
                # Fallback: match D4H email field directly
                d4h_email = ((m.get("email") or {}).get("value") or "").strip().lower()
                if d4h_email and d4h_email == email:
                    result = {"id": str(m["id"]), "name": m.get("name", "")}
                    _me_cache[email] = {"member": result, "ts": now_ts}
                    return result
            total = body.get("totalSize", 0)
            page += 1
            if page * 100 >= total:
                break
    except Exception:
        pass

    _me_cache[email] = {"member": None, "ts": now_ts}
    return None


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

        yield _evt({"status": f"Fetching off-call entries… ({len(members)} members)", "percent": 0})

        today = datetime.now(timezone.utc).date().isoformat()
        after = f"{today}T00:00:00Z"
        member_params = "&".join(f"member_id={mid}" for mid in member_ids)

        all_duties = []
        total_duties = None
        page = 0
        while True:
            url = (
                f"{BASE_URL}/team/{TEAM_ID}/duties"
                f"?after={after}&before=2099-12-31T23:59:59Z"
                f"&{member_params}&page={page}&size=100&order=asc"
            )
            resp = requests.get(url, headers=_headers(), timeout=30)
            resp.raise_for_status()
            body = resp.json()
            batch = body.get("results", [])
            if not batch:
                break
            all_duties.extend(batch)
            if total_duties is None:
                total_duties = body.get("count") or body.get("total") or None
            page += 1
            if total_duties:
                pct = min(99, round(len(all_duties) / total_duties * 100))
                yield _evt({"status": f"Fetching off-call entries… ({len(all_duties)} / {total_duties})", "percent": pct})
            else:
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
            total_unnamed = len(still_unnamed)
            yield _evt({"status": f"Fetching member names… (0 / {total_unnamed})", "percent": 0})
            for i, mid in enumerate(still_unnamed):
                try:
                    url = f"{BASE_URL}/team/{TEAM_ID}/members/{mid}"
                    r = requests.get(url, headers=_headers(), timeout=10)
                    if r.ok:
                        name = r.json().get("name", "")
                        if name:
                            for m in members:
                                if m["id"] == mid:
                                    m["name"] = name
                except Exception:
                    pass
                pct = round((i + 1) / total_unnamed * 100)
                yield _evt({"status": f"Fetching member names… ({i + 1} / {total_unnamed})", "percent": pct})

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


@app.route("/api/me")
def api_me():
    email = _cf_email()
    if email == "local":
        return jsonify({"email": "local", "member": None})
    member = _resolve_me(email)
    return jsonify({"email": email, "member": member})


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


@app.route("/api/members")
def api_members():
    try:
        return jsonify({"members": _fetch_all_members()})
    except requests.HTTPError as e:
        return jsonify({"error": f"D4H API error: {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/unavailable")
def get_unavailable():
    entries = [e for e in _read_unavailable() if not e.get("deleted")]
    return jsonify({"entries": entries})


@app.route("/api/unavailable", methods=["POST"])
def post_unavailable():
    body      = request.get_json() or {}
    members   = body.get("members", [])
    starts_at = (body.get("starts_at") or "").strip()
    ends_at   = (body.get("ends_at") or "").strip()
    notes     = (body.get("notes") or "").strip()

    if not members or not starts_at or not ends_at:
        return jsonify({"error": "members, starts_at, ends_at required"}), 400

    email       = _cf_email()
    now         = datetime.now(timezone.utc).isoformat()
    all_entries = _read_unavailable()
    created     = []

    for member in members:
        entry = {
            "id":          str(uuid.uuid4()),
            "member_id":   str(member.get("id", "")),
            "member_name": member.get("name", ""),
            "starts_at":   starts_at,
            "ends_at":     ends_at,
            "notes":       notes,
            "deleted":     False,
            "history":     [{"action": "created", "by": email, "at": now}],
        }
        all_entries.append(entry)
        created.append({k: entry[k] for k in ("id", "member_id", "member_name", "starts_at", "ends_at", "notes")})

    _write_unavailable(all_entries)
    return jsonify({"created": created})


@app.route("/api/unavailable/<entry_id>", methods=["PUT"])
def put_unavailable(entry_id):
    body      = request.get_json() or {}
    starts_at = body.get("starts_at")
    ends_at   = body.get("ends_at")
    notes     = body.get("notes")

    all_entries = _read_unavailable()
    for entry in all_entries:
        if entry["id"] == entry_id and not entry.get("deleted"):
            before = {
                "starts_at": entry["starts_at"],
                "ends_at":   entry["ends_at"],
                "notes":     entry.get("notes", ""),
            }
            changes = []
            if starts_at is not None and starts_at != before["starts_at"]:
                changes.append(f"Start: {before['starts_at']} → {starts_at}")
                entry["starts_at"] = starts_at
            if ends_at is not None and ends_at != before["ends_at"]:
                changes.append(f"End: {before['ends_at']} → {ends_at}")
                entry["ends_at"] = ends_at
            if notes is not None and notes != before["notes"]:
                changes.append("Notes updated")
                entry["notes"] = notes
            if changes:
                entry["history"].append({
                    "action":  "edited",
                    "by":      _cf_email(),
                    "at":      datetime.now(timezone.utc).isoformat(),
                    "changes": changes,
                    "before":  before,
                })
            _write_unavailable(all_entries)
            return jsonify({"entry": {k: entry[k] for k in ("id", "member_id", "member_name", "starts_at", "ends_at", "notes")}})
    return jsonify({"error": "not found"}), 404


@app.route("/api/unavailable/<entry_id>", methods=["DELETE"])
def delete_unavailable(entry_id):
    all_entries = _read_unavailable()
    for entry in all_entries:
        if entry["id"] == entry_id and not entry.get("deleted"):
            entry["deleted"] = True
            entry["history"].append({
                "action": "deleted",
                "by":     _cf_email(),
                "at":     datetime.now(timezone.utc).isoformat(),
            })
            _write_unavailable(all_entries)
            return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/unavailable/<entry_id>/history")
def get_unavailable_history(entry_id):
    for entry in _read_unavailable():
        if entry["id"] == entry_id:
            return jsonify({
                "history": entry.get("history", []),
                "entry": {
                    "member_name": entry["member_name"],
                    "starts_at":   entry["starts_at"],
                    "ends_at":     entry["ends_at"],
                    "deleted":     entry.get("deleted", False),
                },
            })
    return jsonify({"error": "not found"}), 404


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
