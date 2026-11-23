"""Flask web application for pump cumulative-failure-incidence accounting."""
from __future__ import annotations

import os

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

from . import services
from .csvio import parse_csv
from .db import connect, init_db
from .lifetable import EVENT_LABELS, EVENT_TYPES, PumpRecord
from .matching import MAX_CONTROL_PICKS, MAX_FAILURE_PICKS


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload cap
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = db_path

    def db_conn():
        from flask import g
        if "db" not in g:
            g.db = connect(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc):
        from flask import g
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.route("/health")
    def health():
        return jsonify(status="ok")

    # ------------------------------------------------------------- dashboard
    @app.route("/")
    def index():
        conn = db_conn()
        meta = services.get_meta(conn)
        table = services.get_life_table(conn)
        return render_template("index.html", meta=meta, table=table,
                               event_labels=EVENT_LABELS)

    # -------------------------------------------------------------- CSV upload
    @app.route("/upload", methods=["POST"])
    def upload():
        file = request.files.get("csv_file")
        if file is None or file.filename == "":
            flash("请先选择要上传的 CSV 文件", "error")
            return redirect(url_for("index"))
        raw = file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gbk")
            except UnicodeDecodeError:
                flash("CSV 编码无法识别，请保存为 UTF-8 后重试", "error")
                return redirect(url_for("index"))
        try:
            records = parse_csv(text)
            result = services.replace_ledger(db_conn(), records,
                                             source=f"csv:{file.filename}")
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"写入失败，已保留原有台账与汇总: {exc}", "error")
            return redirect(url_for("index"))
        roster_note = ""
        if result.get("pair_count") is not None:
            roster_note = f"；现行名单已按原选泵号与工时差上限重算，配成 {result['pair_count']} 对"
        flash(f"上传成功：台账已整体替换为 {result['pump_count']} 台泵，"
              f"汇总已同步重算{roster_note}",
              "ok")
        return redirect(url_for("index"))

    # ------------------------------------------------------------- pump ledger
    @app.route("/pumps")
    def pumps():
        rows = services.list_pumps(db_conn())
        return render_template("pumps.html", pumps=rows,
                               event_labels=EVENT_LABELS)

    @app.route("/pumps/new", methods=["GET", "POST"])
    def pump_new():
        return _pump_form(None)

    @app.route("/pumps/<pump_id>/edit", methods=["GET", "POST"])
    def pump_edit(pump_id: str):
        row = services.get_pump(db_conn(), pump_id)
        if row is None:
            abort(404)
        return _pump_form(row)

    def _pump_form(existing):
        conn = db_conn()
        if request.method == "GET":
            return render_template(
                "pump_form.html",
                existing=existing,
                event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS,
                form=dict(existing) if existing else {},
                errors=[],
            )
        form, record, errors = _read_pump_form(request.form,
                                               fixed_id=existing["pump_id"] if existing else None)
        if errors:
            return render_template(
                "pump_form.html", existing=existing, event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS, form=form, errors=errors,
            ), 400
        try:
            result = services.upsert_pump(conn, record)
        except ValueError as exc:
            return render_template(
                "pump_form.html", existing=existing, event_types=EVENT_TYPES,
                event_labels=EVENT_LABELS, form=form, errors=[str(exc)],
            ), 400
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"保存失败，已保留原值: {exc}", "error")
            return redirect(url_for("pumps"))
        label = "更正" if result["action"] == "updated" else "新增"
        pair_note = ""
        if result.get("pair_count") is not None:
            pair_note = f"；现行名单已按原选泵号与工时差上限重算，配成 {result['pair_count']} 对"
        flash(f"泵 {record.pump_id} {label}成功，台账、汇总与名单已在同一事务内生效{pair_note}", "ok")
        return redirect(url_for("pumps"))

    def _read_pump_form(form, fixed_id=None):
        errors = []
        pump_id = (fixed_id or form.get("pump_id", "")).strip()
        if not pump_id:
            errors.append("泵号不能为空")
        try:
            entry = float(form.get("entry_hours", ""))
            if entry < 0:
                errors.append("入册小时数不能为负")
        except ValueError:
            entry = None
            errors.append("入册小时数必须是数字")
        try:
            exit_hours = float(form.get("exit_hours", ""))
        except ValueError:
            exit_hours = None
            errors.append("出册小时数必须是数字")
        event_type = form.get("event_type", "")
        if event_type not in EVENT_TYPES:
            errors.append("结束方式必须是 故障 / 预防更换 / 观察结束 之一")
        if entry is not None and exit_hours is not None and exit_hours < entry:
            errors.append(f"出册小时数 ({exit_hours}) 不能早于入册小时数 ({entry})")
        record = None
        if not errors:
            record = PumpRecord(pump_id, entry, exit_hours, event_type)
        return dict(form), record, errors

    # --------------------------------------------------- case-control roster
    @app.route("/roster", methods=["GET"])
    def roster_page():
        conn = db_conn()
        roster = services.get_roster(conn)
        pumps = services.list_pumps(conn)
        selected_failures = {f["pump_id"] for f in roster["failures"]} if roster else set()
        selected_candidates = {c["pump_id"] for c in roster["candidates"]} if roster else set()
        max_diff = roster["max_diff_hours"] if roster else None
        return render_template(
            "roster.html",
            roster=roster,
            pumps=pumps,
            event_labels=EVENT_LABELS,
            max_failure_picks=MAX_FAILURE_PICKS,
            max_control_picks=MAX_CONTROL_PICKS,
            selected_failures=selected_failures,
            selected_candidates=selected_candidates,
            max_diff=max_diff,
        )

    @app.route("/roster/save", methods=["POST"])
    def roster_save():
        failure_ids = request.form.getlist("failure_ids")
        candidate_ids = request.form.getlist("candidate_ids")
        raw_diff = request.form.get("max_diff_hours", "").strip()
        try:
            max_diff = float(raw_diff)
        except ValueError:
            flash("入册工时差上限必须是非负数字", "error")
            return redirect(url_for("roster_page"))
        try:
            result = services.save_roster(db_conn(), failure_ids,
                                          candidate_ids, max_diff)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("roster_page"))
        except Exception as exc:  # pragma: no cover - defensive
            flash(f"名单保存失败，已沿用原名单: {exc}", "error")
            return redirect(url_for("roster_page"))
        flash(
            f"名单已保存：故障泵 {result['failure_count']} 台、"
            f"候选对照 {result['candidate_count']} 台、工时差上限 "
            f"{result['max_diff_hours']:g} 小时；配成 {result['pair_count']} 对，"
            f"其中保留上次配对 {result['retained_count']} 对。"
            f"台账、汇总与名单已在同一事务内生效",
            "ok",
        )
        return redirect(url_for("roster_page"))

    @app.route("/api/roster")
    def api_roster():
        roster = services.get_roster(db_conn())
        if roster is None:
            return jsonify(error="尚未保存任何名单"), 404
        return jsonify(roster)

    # ---------------------------------------------------- read-only query page
    @app.route("/query", methods=["GET"])
    def query_page():
        hours_raw = request.args.get("hours", "").strip()
        if hours_raw == "":
            return render_template("query.html", result=None, hours_raw="",
                                   error=None)
        try:
            hours = float(hours_raw)
            if hours < 0:
                raise ValueError
        except ValueError:
            return render_template(
                "query.html", result=None, hours_raw=hours_raw,
                error="请输入非负数字小时数",
            ), 400
        result = services.query_incidence(db_conn(), hours)
        return render_template("query.html", result=result,
                               hours_raw=hours_raw, error=None,
                               event_labels=EVENT_LABELS)

    @app.route("/api/query")
    def api_query():
        hours_raw = request.args.get("hours", "").strip()
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            return jsonify(error="参数 hours 必须是数字"), 400
        if hours < 0:
            return jsonify(error="参数 hours 不能为负"), 400
        return jsonify(services.query_incidence(db_conn(), hours))

    @app.route("/api/lifetable")
    def api_lifetable():
        return jsonify([dict(r) for r in services.get_life_table(db_conn())])

    with app.app_context():
        init_db(db_conn())

    return app
