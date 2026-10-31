"""Persistent tax comparisons and structured, sanitized operational logs."""

import json
import re
import time
from datetime import datetime, timezone, timedelta
from .tax_review import append_snapshot

AMOUNTS = {
    "trade_income_cents": "交易收入",
    "coupon_subsidy_cents": "优惠券补贴",
    "refund_cents": "退款",
    "coupon_refund_cents": "退款退优惠券",
    "tax_amount_cents": "报税金额",
}
ENTITY = {
    "company_name": "公司名称",
    "credit_code": "统一社会信用代码",
    "taxpayer_number": "纳税人识别号",
    "legal_representative": "法定代表人",
    "registered_address": "注册地址",
    "business_address": "经营地址",
    "company_type": "公司类型",
    "registration_status": "登记状态",
    "business_scope": "经营范围",
    "license_start": "营业期限起",
    "license_end": "营业期限止",
    "subject_type": "主体类型",
}


def clean_message(message):
    message = re.sub(r"https?://\S+", "[地址已隐藏]", str(message))
    message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[已隐藏]", message)
    message = re.sub(
        r"""(?i)((?:password|passwd|token|cookie|authorization|secret|密码|密钥)["']?\s*[=:：]\s*)("[^"]*"|'[^']*'|[^\s,;}]+)""",
        r"\1[已隐藏]",
        message,
    )
    return message[:500]


def differences(before, after):
    result = []
    from .platform_tax import FIELDS

    for field, label in {**AMOUNTS, **{k + "_cents": v for k, v in FIELDS.items()}}.items():
        if before.get(field) != after.get(field):
            result.append(
                {"field": field, "label": label, "before": before.get(field), "after": after.get(field), "money": True}
            )
    for field, label in ENTITY.items():
        a = (before.get("entity") or {}).get(field) or ""
        b = (after.get("entity") or {}).get(field) or ""
        if a != b:
            result.append({"field": field, "label": label, "before": a, "after": b, "money": False})
    return result


def comparison(period, diff, missing=False, now=None, row=None, previous=None):
    current = period == datetime.fromtimestamp(
        time.time() if now is None else now, timezone(timedelta(hours=8))
    ).strftime("%Y-%m")
    for d in diff:
        d["highlight"] = (
            (d["before"] is not None and d["after"] is not None and d["after"] < d["before"])
            if d["money"]
            else d["field"] == "company_name"
        )
    warnings = []
    if any(d["money"] and d["highlight"] for d in diff):
        warnings.append("金额较上一版减少")
    if any(not d["money"] and d["highlight"] for d in diff):
        warnings.append("公司名称发生变化")
    return {
        "changed": bool(diff),
        "attention": bool(warnings),
        "warnings": warnings,
        "current_month": current,
        "routine_update": bool(diff) and not warnings,
        "missing": missing,
        "differences": diff,
        "previous": previous if diff else None,
        "company_changed": any(d["field"] == "company_name" and d["highlight"] for d in diff),
    }


class AuditStore:
    def initialize_audit(self):
        with self.connect() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS tax_records (
                shop_id INTEGER NOT NULL, profile_version INTEGER NOT NULL, period TEXT NOT NULL,
                baseline TEXT NOT NULL, latest TEXT NOT NULL, baseline_at REAL NOT NULL, checked_at REAL NOT NULL,
                PRIMARY KEY(shop_id,profile_version,period));
                CREATE TABLE IF NOT EXISTS log_spool_cursors (file TEXT PRIMARY KEY,offset INTEGER NOT NULL,lines INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, last_seen REAL NOT NULL,
                level TEXT NOT NULL, module TEXT NOT NULL, event TEXT NOT NULL, shop_id INTEGER,
                shop_name TEXT NOT NULL, message TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS event_logs_recent ON event_logs(last_seen DESC);
                CREATE INDEX IF NOT EXISTS event_logs_cursor ON event_logs(last_seen DESC,id DESC);
                CREATE INDEX IF NOT EXISTS event_logs_module_cursor ON event_logs(module,last_seen DESC,id DESC);
                CREATE INDEX IF NOT EXISTS tax_entity_latest ON tax_records(shop_id,profile_version,checked_at DESC,period DESC);""")
            columns = {r["name"] for r in db.execute("PRAGMA table_info(event_logs)")}
            if "platform" not in columns:
                db.execute("ALTER TABLE event_logs ADD COLUMN platform TEXT NOT NULL DEFAULT ''")
            if "platform_name" not in columns:
                db.execute("ALTER TABLE event_logs ADD COLUMN platform_name TEXT NOT NULL DEFAULT ''")
            if not db.execute("SELECT 1 FROM settings WHERE key='tax_baseline_migrated'").fetchone():
                # Use the latest previously saved month, independent of query scope.
                for cached in db.execute(
                    "SELECT c.*,s.profile_version FROM business_cache c JOIN shops s ON c.shop_id=s.id WHERE cache_key LIKE 'tax:%' ORDER BY updated"
                ):
                    for row in json.loads(cached["payload"]).get("rows", []):
                        if not row.get("period") or "tax_amount_cents" not in row:
                            continue
                        raw = json.dumps(row, ensure_ascii=False)
                        db.execute(
                            "INSERT OR REPLACE INTO tax_records(shop_id,profile_version,period,baseline,latest,baseline_at,checked_at) VALUES(?,?,?,?,?,?,?)",
                            (
                                cached["shop_id"],
                                cached["profile_version"],
                                row["period"],
                                raw,
                                raw,
                                cached["updated"],
                                cached["updated"],
                            ),
                        )
                db.execute("INSERT INTO settings(key,value) VALUES('tax_baseline_migrated','1')")

    def compare_tax(self, shop_id, payload, options):
        import hashlib

        now = time.time()
        rows = []
        with self.connect() as db:
            shop = db.execute("SELECT profile_version FROM shops WHERE id=?", (shop_id,)).fetchone()
            if not shop:
                return payload
            version = shop["profile_version"]
            seen = set()
            for incoming in payload["rows"]:
                row = {k: v for k, v in incoming.items() if k != "db_comparison"}
                key = row["period"]
                if row.get("official_tax"):
                    identity = (
                        row["period"]
                        if sum(r["period"] == row["period"] for r in payload["rows"]) == 1
                        else str(row["id"])
                    )
                    key = row["created_at"][:7] + "~" + hashlib.sha256(identity.encode()).hexdigest()[:24]
                seen.add(key)
                args = (shop_id, version, key)
                old = db.execute(
                    "SELECT * FROM tax_records WHERE shop_id=? AND profile_version=? AND period=?", args
                ).fetchone()
                previous = json.loads(old["baseline"]) if old else None
                previous_at = old["baseline_at"] if old else now
                changed = False
                if old:
                    latest = json.loads(old["latest"])
                    latest.pop("_missing_from_source", None)
                    if differences(latest, row):
                        previous = latest
                        previous_at = old["checked_at"]
                        changed = True
                raw = json.dumps(row, ensure_ascii=False)
                baseline = json.dumps(previous or row, ensure_ascii=False)
                db.execute(
                    "INSERT OR REPLACE INTO tax_records(shop_id,profile_version,period,baseline,latest,baseline_at,checked_at) VALUES(?,?,?,?,?,?,?)",
                    (*args, baseline, raw, previous_at, now),
                )
                # One historical value; unchanged polling does not rotate it.
                if changed or not old:
                    db.execute("DELETE FROM tax_history WHERE shop_id=? AND profile_version=? AND period=?", args)
                    if previous and differences(previous, row):
                        append_snapshot(db, *args, previous, previous_at)
                rows.append(
                    {
                        **row,
                        "db_comparison": {
                            **comparison(
                                row["period"], differences(previous or row, row), row=row, previous=previous, now=now
                            ),
                            "baseline_at": previous_at,
                            "checked_at": now,
                        },
                    }
                )
            for old in db.execute(
                "SELECT * FROM tax_records WHERE shop_id=? AND profile_version=? AND period>=? AND period<?",
                (shop_id, version, options["date_from"][:7], options["date_to"][:7]),
            ):
                if old["period"] in seen:
                    continue
                row = json.loads(old["latest"])
                if row.get("official_tax"):
                    continue
                row["_missing_from_source"] = True
                db.execute(
                    "UPDATE tax_records SET latest=?,checked_at=? WHERE shop_id=? AND profile_version=? AND period=?",
                    (json.dumps(row, ensure_ascii=False), now, shop_id, version, old["period"]),
                )
                previous = json.loads(old["baseline"])
                rows.append(
                    {
                        **row,
                        "db_comparison": {
                            **comparison(
                                row["period"], differences(previous, row), missing=True, row=row, previous=previous
                            ),
                            "baseline_at": old["baseline_at"],
                            "checked_at": now,
                        },
                    }
                )
        return {**payload, "rows": rows, "database_compared": True}

    def tax_snapshot(self, shop_id, options):
        rows = []
        with self.connect() as db:
            records = db.execute(
                "SELECT t.*,s.platform FROM tax_records t JOIN shops s ON t.shop_id=s.id AND t.profile_version=s.profile_version WHERE t.shop_id=? AND t.period>=? AND t.period<? ORDER BY t.period DESC",
                (shop_id, options["date_from"][:7], options["date_to"][:7]),
            )
            for record in records:
                row = json.loads(record["latest"])
                missing = row.pop("_missing_from_source", False)
                if (
                    row.get("official_tax")
                    and record["platform"] in ("taobao", "jd")
                    and row["period"] != options["period"]
                ):
                    continue
                previous = json.loads(record["baseline"])
                rows.append(
                    {
                        **row,
                        "db_comparison": {
                            **comparison(
                                row["period"], differences(previous, row), missing=missing, row=row, previous=previous
                            ),
                            "baseline_at": record["baseline_at"],
                            "checked_at": record["checked_at"],
                        },
                    }
                )
        return rows

    def log_event(self, level, module, event, message, shop_id=None):
        from .log_spool import get
        from .log_identity import identity

        if level == "info" and event in ("sync_yield", "sync_start"):
            return
        message = clean_message(message)
        name, platform = identity(self.path, shop_id)
        get(self).append(
            {
                "created": time.time(),
                "level": level,
                "module": module,
                "event": event,
                "message": message,
                "shop_id": shop_id,
                "shop_name": name,
                "platform": platform,
            }
        )
        try:
            from .collector_logs import shop_event

            shop_event(self.path.parent / "launcher-logs", shop_id, level, module, event, message, name)
            if not shop_id and level in ("error", "critical"):
                from .error_log import append

                append(self.path.parent / "launcher-logs", f"{level.upper()} [{module}] {message}")
        except OSError:
            pass

    def read_logs(
        self,
        level="",
        shop_id=None,
        keyword="",
        limit=100,
        module="",
        date_from="",
        date_to="",
        before=0,
        before_id=0,
        after=0,
    ):
        from .file_log_query import read_logs

        return read_logs(self, level, shop_id, keyword, limit, module, date_from, date_to, before, before_id, after)
