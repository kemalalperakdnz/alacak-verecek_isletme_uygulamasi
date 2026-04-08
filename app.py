from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from flask import Flask, flash, redirect, render_template, request, send_file, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
LOGO_PATH = BASE_DIR / "logo.png"


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

    CARD_FEE_RATE = 0.20

    def get_db() -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db() -> None:
        conn = get_db()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_date TEXT NOT NULL,
                    entry_type TEXT NOT NULL CHECK(entry_type IN ('income','expense')),
                    amount REAL NOT NULL CHECK(amount >= 0),
                    note TEXT,
                    payment_method TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date)")

            cols = {row["name"] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
            if "payment_method" not in cols:
                conn.execute("ALTER TABLE entries ADD COLUMN payment_method TEXT")
                conn.execute(
                    "UPDATE entries SET payment_method = 'cash' WHERE entry_type = 'income' AND payment_method IS NULL"
                )
            conn.commit()
        finally:
            conn.close()

    def month_start_end(ym: str) -> tuple[str, str]:
        # ym: YYYY-MM
        start = datetime.strptime(ym + "-01", "%Y-%m-%d").date()
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        end = next_month
        return start.isoformat(), end.isoformat()

    def default_month() -> str:
        today = date.today()
        return f"{today.year:04d}-{today.month:02d}"

    def parse_amount(raw: str) -> float | None:
        if raw is None:
            return None
        s = raw.strip().replace(" ", "")
        if not s:
            return None
        s = s.replace(",", ".")
        try:
            val = float(s)
        except ValueError:
            return None
        if val < 0:
            return None
        return val

    @app.get("/logo.png")
    def logo() -> object:
        if not LOGO_PATH.exists():
            return ("Logo bulunamadı", 404)
        return send_file(LOGO_PATH)

    @app.get("/")
    def index() -> str:
        ym = request.args.get("month") or default_month()
        type_filter = (request.args.get("type") or "").strip()
        payment_filter = (request.args.get("payment") or "").strip()
        q = (request.args.get("q") or "").strip()
        sort = (request.args.get("sort") or "date_desc").strip()

        if type_filter not in {"", "income", "expense"}:
            type_filter = ""
        if payment_filter not in {"", "cash", "card"}:
            payment_filter = ""

        sort_map = {
            "date_desc": "entry_date DESC, id DESC",
            "date_asc": "entry_date ASC, id ASC",
            "amount_desc": "amount DESC, id DESC",
            "amount_asc": "amount ASC, id ASC",
        }
        order_by = sort_map.get(sort, sort_map["date_desc"])
        if sort not in sort_map:
            sort = "date_desc"

        try:
            start, end = month_start_end(ym)
        except ValueError:
            ym = default_month()
            start, end = month_start_end(ym)

        conn = get_db()
        try:
            where = ["entry_date >= ? AND entry_date < ?"]
            params: list[object] = [start, end]

            if type_filter:
                where.append("entry_type = ?")
                params.append(type_filter)

            if payment_filter:
                where.append("entry_type = 'income'")
                where.append("COALESCE(payment_method,'cash') = ?")
                params.append(payment_filter)

            if q:
                where.append("COALESCE(note,'') LIKE ?")
                params.append(f"%{q}%")

            where_sql = " AND ".join(where)

            rows = conn.execute(
                f"""
                SELECT id, entry_date, entry_type, amount, note, payment_method
                FROM entries
                WHERE {where_sql}
                ORDER BY {order_by}
                """ ,
                params,
            ).fetchall()

            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='cash' THEN amount END), 0) AS cash_income,
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='card' THEN amount END), 0) AS card_income,
                    COALESCE(SUM(CASE WHEN entry_type='expense' THEN amount END), 0) AS total_expense
                FROM entries
                WHERE entry_date >= ? AND entry_date < ?
                """,
                (start, end),
            ).fetchone()
        finally:
            conn.close()

        cash_income = float(totals["cash_income"])
        card_income = float(totals["card_income"])
        total_expense = float(totals["total_expense"])

        card_fee = card_income * CARD_FEE_RATE
        net_income = cash_income + (card_income - card_fee)
        profit = net_income - total_expense

        return render_template(
            "index.html",
            month=ym,
            entries=rows,
            cash_income=cash_income,
            card_income=card_income,
            card_fee=card_fee,
            net_income=net_income,
            total_expense=total_expense,
            profit=profit,
            today=date.today().isoformat(),
            type_filter=type_filter,
            payment_filter=payment_filter,
            q=q,
            sort=sort,
        )

    @app.post("/add")
    def add() -> object:
        entry_date = (request.form.get("entry_date") or "").strip()
        entry_type = (request.form.get("entry_type") or "").strip()
        payment_method = (request.form.get("payment_method") or "").strip()
        amount_raw = request.form.get("amount")
        note = (request.form.get("note") or "").strip() or None
        month = (request.form.get("month") or "").strip() or default_month()

        if not entry_date:
            flash("Tarih zorunludur.", "danger")
            return redirect(url_for("index", month=month))

        try:
            datetime.strptime(entry_date, "%Y-%m-%d")
        except ValueError:
            flash("Tarih formatı geçersiz. (YYYY-AA-GG)", "danger")
            return redirect(url_for("index", month=month))

        if entry_type not in {"income", "expense"}:
            flash("Tür seçimi geçersiz.", "danger")
            return redirect(url_for("index", month=month))

        if entry_type == "income":
            if payment_method not in {"cash", "card"}:
                flash("Ödeme türü seçimi geçersiz.", "danger")
                return redirect(url_for("index", month=month))
        else:
            payment_method = None

        amount = parse_amount(amount_raw or "")
        if amount is None:
            flash("Tutar geçersiz.", "danger")
            return redirect(url_for("index", month=month))

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO entries(entry_date, entry_type, amount, note, payment_method) VALUES (?,?,?,?,?)",
                (entry_date, entry_type, amount, note, payment_method),
            )
            conn.commit()
        finally:
            conn.close()

        flash("Kayıt eklendi.", "success")
        return redirect(url_for("index", month=month))

    @app.route("/edit/<int:entry_id>", methods=["GET", "POST"])
    def edit(entry_id: int) -> object:
        month = (request.values.get("month") or "").strip() or default_month()

        conn = get_db()
        try:
            entry = conn.execute(
                "SELECT id, entry_date, entry_type, amount, note, payment_method FROM entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        finally:
            conn.close()

        if entry is None:
            flash("Kayıt bulunamadı.", "danger")
            return redirect(url_for("index", month=month))

        if request.method == "GET":
            return render_template("edit.html", entry=entry, month=month)

        entry_date = (request.form.get("entry_date") or "").strip()
        entry_type = (request.form.get("entry_type") or "").strip()
        payment_method = (request.form.get("payment_method") or "").strip()
        amount_raw = request.form.get("amount")
        note = (request.form.get("note") or "").strip() or None

        if not entry_date:
            flash("Tarih zorunludur.", "danger")
            return redirect(url_for("edit", entry_id=entry_id, month=month))

        try:
            datetime.strptime(entry_date, "%Y-%m-%d")
        except ValueError:
            flash("Tarih formatı geçersiz. (YYYY-AA-GG)", "danger")
            return redirect(url_for("edit", entry_id=entry_id, month=month))

        if entry_type not in {"income", "expense"}:
            flash("Tür seçimi geçersiz.", "danger")
            return redirect(url_for("edit", entry_id=entry_id, month=month))

        if entry_type == "income":
            if payment_method not in {"cash", "card"}:
                flash("Ödeme türü seçimi geçersiz.", "danger")
                return redirect(url_for("edit", entry_id=entry_id, month=month))
        else:
            payment_method = None

        amount = parse_amount(amount_raw or "")
        if amount is None:
            flash("Tutar geçersiz.", "danger")
            return redirect(url_for("edit", entry_id=entry_id, month=month))

        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE entries
                SET entry_date = ?, entry_type = ?, amount = ?, note = ?, payment_method = ?
                WHERE id = ?
                """,
                (entry_date, entry_type, amount, note, payment_method, entry_id),
            )
            conn.commit()
        finally:
            conn.close()

        flash("Kayıt güncellendi.", "success")
        return redirect(url_for("index", month=month))

    @app.post("/delete/<int:entry_id>")
    def delete(entry_id: int) -> object:
        month = (request.form.get("month") or "").strip() or default_month()
        conn = get_db()
        try:
            conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            conn.commit()
        finally:
            conn.close()
        flash("Kayıt silindi.", "secondary")
        return redirect(url_for("index", month=month))

    @app.get("/report")
    def report() -> str:
        ym = request.args.get("month") or default_month()
        try:
            start, end = month_start_end(ym)
        except ValueError:
            ym = default_month()
            start, end = month_start_end(ym)

        conn = get_db()
        try:
            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='cash' THEN amount END), 0) AS cash_income,
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='card' THEN amount END), 0) AS card_income,
                    COALESCE(SUM(CASE WHEN entry_type='expense' THEN amount END), 0) AS total_expense,
                    COUNT(*) as count_entries
                FROM entries
                WHERE entry_date >= ? AND entry_date < ?
                """,
                (start, end),
            ).fetchone()

            daily = conn.execute(
                """
                SELECT entry_date,
                       COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='cash' THEN amount END), 0) AS cash_income,
                       COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='card' THEN amount END), 0) AS card_income,
                       COALESCE(SUM(CASE WHEN entry_type='expense' THEN amount END), 0) AS expense,
                       GROUP_CONCAT(COALESCE(note,''), ' | ') AS notes
                FROM entries
                WHERE entry_date >= ? AND entry_date < ?
                GROUP BY entry_date
                ORDER BY entry_date DESC
                """,
                (start, end),
            ).fetchall()
        finally:
            conn.close()

        cash_income = float(totals["cash_income"])
        card_income = float(totals["card_income"])
        total_expense = float(totals["total_expense"])

        card_fee = card_income * CARD_FEE_RATE
        net_income = cash_income + (card_income - card_fee)
        profit = net_income - total_expense

        return render_template(
            "report.html",
            month=ym,
            cash_income=cash_income,
            card_income=card_income,
            card_fee=card_fee,
            net_income=net_income,
            total_expense=total_expense,
            profit=profit,
            count_entries=int(totals["count_entries"]),
            daily=daily,
        )

    @app.get("/yearly")
    def yearly() -> str:
        year_raw = (request.args.get("year") or "").strip()
        this_year = date.today().year
        if year_raw.isdigit() and len(year_raw) == 4:
            year = int(year_raw)
        else:
            year = this_year

        month_names_tr = [
            "Ocak",
            "Şubat",
            "Mart",
            "Nisan",
            "Mayıs",
            "Haziran",
            "Temmuz",
            "Ağustos",
            "Eylül",
            "Ekim",
            "Kasım",
            "Aralık",
        ]

        year_start = date(year, 1, 1).isoformat()
        year_end = date(year + 1, 1, 1).isoformat()

        conn = get_db()
        try:
            monthly_rows = conn.execute(
                """
                SELECT
                    substr(entry_date, 1, 7) AS ym,
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='cash' THEN amount END), 0) AS cash_income,
                    COALESCE(SUM(CASE WHEN entry_type='income' AND COALESCE(payment_method,'cash')='card' THEN amount END), 0) AS card_income,
                    COALESCE(SUM(CASE WHEN entry_type='expense' THEN amount END), 0) AS total_expense,
                    COUNT(*) as count_entries
                FROM entries
                WHERE entry_date >= ? AND entry_date < ?
                GROUP BY substr(entry_date, 1, 7)
                ORDER BY ym ASC
                """,
                (year_start, year_end),
            ).fetchall()
        finally:
            conn.close()

        by_month: dict[str, dict[str, float | int]] = {}
        for r in monthly_rows:
            by_month[str(r["ym"])]= {
                "cash_income": float(r["cash_income"]),
                "card_income": float(r["card_income"]),
                "total_expense": float(r["total_expense"]),
                "count_entries": int(r["count_entries"]),
            }

        months: list[dict[str, object]] = []
        totals_cash = 0.0
        totals_card = 0.0
        totals_expense = 0.0
        totals_count = 0

        for m in range(1, 13):
            ym = f"{year:04d}-{m:02d}"
            data = by_month.get(
                ym,
                {"cash_income": 0.0, "card_income": 0.0, "total_expense": 0.0, "count_entries": 0},
            )
            cash_income = float(data["cash_income"])
            card_income = float(data["card_income"])
            total_expense = float(data["total_expense"])
            count_entries = int(data["count_entries"])

            card_fee = card_income * CARD_FEE_RATE
            net_income = cash_income + (card_income - card_fee)
            profit = net_income - total_expense

            totals_cash += cash_income
            totals_card += card_income
            totals_expense += total_expense
            totals_count += count_entries

            months.append(
                {
                    "ym": ym,
                    "month_name": month_names_tr[m - 1],
                    "cash_income": cash_income,
                    "card_income": card_income,
                    "card_fee": card_fee,
                    "net_income": net_income,
                    "total_expense": total_expense,
                    "profit": profit,
                    "count_entries": count_entries,
                }
            )

        totals_card_fee = totals_card * CARD_FEE_RATE
        totals_net_income = totals_cash + (totals_card - totals_card_fee)
        totals_profit = totals_net_income - totals_expense

        return render_template(
            "yearly.html",
            year=year,
            months=months,
            totals_cash=totals_cash,
            totals_card=totals_card,
            totals_card_fee=totals_card_fee,
            totals_net_income=totals_net_income,
            totals_expense=totals_expense,
            totals_profit=totals_profit,
            totals_count=totals_count,
        )

    init_db()
    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
