# -*- coding: utf-8 -*-
#
# Read-only JSON API for bup, mounted at /api/v1 (see app.register_blueprint).
#
# Consumers: on-wiki gadgets + external bots. So: read-only (all writes stay in
# the human OAuth UI), CORS-open, versioned, stable schema. The data is
# CANDIDATES, not commands: `newcite` assumes the live article still contains
# `oldcite` verbatim, so a consumer must match against current article text
# before applying (exactly what bup itself does).

import os
from datetime import datetime

import flask
from flask import Blueprint, jsonify, request

import db as dbmod
import bookbot

api = Blueprint("api", __name__)
API_VERSION = "v1"

MAX_LIMIT = 500
DEFAULT_LIMIT = 50


@api.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _conn():
    # Reuses the app's per-request connection + teardown (app.close_db pops
    # flask.g["db"]), so we share one connection and don't leak.
    if "db" not in flask.g:
        flask.g.db = dbmod.connect()
    return flask.g.db


def _log_hit(endpoint):
    """Append one line per API call for the daily stats job (stats.py).
    Format: '<utc-iso> <endpoint>'. Append-only; never read by the app."""
    try:
        path = os.path.join(dbmod.data_dir(), "api_hits.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.utcnow().isoformat(), endpoint))
    except OSError:
        pass


def _int_arg(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


def _citation_out(c):
    return {
        "oldcite": c.get("oldcite", ""),
        "newcite": c.get("newcite", ""),
        "iaid": c.get("iaid", ""),
        "meta": c.get("meta", ""),
        "url": bookbot.archive_url(c),
        "type": dbmod.citation_type(c),
    }


def _page_brief(r):
    return {
        "title": r["page"],
        "counts": {"book": r["book_count"], "sim": r["sim_count"],
                   "ref": r["ref_count"], "total": r["count"]},
    }


@api.route("/health")
def health():
    return jsonify({"status": "ok", "service": "bup", "api": API_VERSION})


@api.route("/stats")
def stats():
    s = dbmod.stats(_conn())
    return jsonify({
        "pages": s["pages"],
        "citations": s["citations"],
        "by_type": {"book": s["book"], "sim": s["sim"], "ref": s["ref"]},
        "note": "Counts of OPEN work only; applied/resolved citations are pruned.",
    })


@api.route("/page/<path:title>")
def page(title):
    _log_hit("page")
    title = title.replace("_", " ").strip()
    rec = dbmod.get_page_by_title(_conn(), title)
    if rec is None:
        return jsonify({"title": title, "found": False, "citations": []}), 404
    return jsonify({
        "title": rec["page"],
        "found": True,
        "counts": {
            "book": rec["book_count"], "sim": rec["sim_count"],
            "ref": rec["ref_count"], "total": rec["count"],
        },
        "citations": [_citation_out(c) for c in rec["citations"]],
    })


@api.route("/worklist")
def worklist():
    _log_hit("worklist")
    limit = _int_arg("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, 10 ** 9)
    min_count = _int_arg("min_count", 0, 0, 10 ** 9)
    ctype = request.args.get("type")
    if ctype not in ("book", "sim", "ref"):
        ctype = None
    rows = dbmod.worklist_page(_conn(), limit=limit, offset=offset,
                               min_count=min_count, ctype=ctype)
    return jsonify({
        "limit": limit, "offset": offset,
        "type": ctype, "min_count": min_count,
        "count": len(rows),
        "pages": [_page_brief(r) for r in rows],
    })


@api.route("/random")
def random():
    _log_hit("random")
    limit = _int_arg("limit", 1, 1, 50)
    min_count = _int_arg("min_count", 0, 0, 10 ** 9)
    ctype = request.args.get("type")
    if ctype not in ("book", "sim", "ref"):
        ctype = None
    rows = dbmod.random_pages(_conn(), limit=limit, min_count=min_count, ctype=ctype)
    return jsonify({"count": len(rows), "pages": [_page_brief(r) for r in rows]})


# Max titles accepted per /pages request; the client chunks larger sets.
PAGES_MAX = 2000


@api.route("/pages", methods=["POST"])
def pages():
    """Given a list of titles, return the subset that's in the worklist (+counts).
    Powers watchlist/category intersection in the gadget. Accepts either a
    newline-separated text/plain body (CORS-safelisted -> no preflight) or
    JSON {"titles": [...]}."""
    _log_hit("pages")
    titles = None
    data = request.get_json(silent=True)
    if isinstance(data, dict) and isinstance(data.get("titles"), list):
        titles = data["titles"]
    else:
        titles = request.get_data(as_text=True).splitlines()

    seen, norm = set(), []
    for t in titles:
        t = ( t or "" ).replace("_", " ").strip()
        if t and t not in seen:
            seen.add(t)
            norm.append(t)
        if len(norm) >= PAGES_MAX:
            break

    rows = dbmod.pages_present(_conn(), norm)
    return jsonify({
        "requested": len(norm), "found": len(rows),
        "pages": [_page_brief(r) for r in rows],
    })
