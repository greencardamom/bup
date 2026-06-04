# -*- coding: utf-8 -*-
#
# Read-only JSON API for bup, mounted at /api/v1 (see app.register_blueprint).
#
# Consumers: on-wiki gadgets + external bots. So: read-only (all writes stay in
# the human OAuth UI), CORS-open, versioned, stable schema. The data is
# CANDIDATES, not commands: `newcite` assumes the live article still contains
# `oldcite` verbatim, so a consumer must match against current article text
# before applying (exactly what bup itself does).

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
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


def _conn():
    # Reuses the app's per-request connection + teardown (app.close_db pops
    # flask.g["db"]), so we share one connection and don't leak.
    if "db" not in flask.g:
        flask.g.db = dbmod.connect()
    return flask.g.db


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
        "pages": [{
            "title": r["page"],
            "counts": {"book": r["book_count"], "sim": r["sim_count"],
                       "ref": r["ref_count"], "total": r["count"]},
        } for r in rows],
    })
