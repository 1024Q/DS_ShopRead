PLATFORMS = {
    "pdd": "拼多多",
    "taobao": "淘宝",
    "jd": "京东",
    "douyin": "抖店",
    "taofactory": "淘工厂",
    "xiaohongshu": "小红书",
    "wechat": "微信小店",
    "other": "其他平台",
}


def validate_platform(value):
    if value not in PLATFORMS:
        raise ValueError("平台分类无效")
    return value


def migrate(db):
    columns = {r["name"] for r in db.execute("PRAGMA table_info(shops)")}
    if "platform" not in columns:
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='shops'").fetchone()
        db.execute("""CREATE TABLE shops_platform (
            id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL COLLATE NOCASE,password BLOB NOT NULL,
            browser TEXT NOT NULL DEFAULT 'system',alias TEXT NOT NULL DEFAULT '',
            profile_version INTEGER NOT NULL DEFAULT 0,platform TEXT NOT NULL DEFAULT 'pdd',
            UNIQUE(platform,username))""")
        db.execute(
            "INSERT INTO shops_platform(id,name,username,password,browser,alias,profile_version) SELECT id,name,username,password,browser,alias,profile_version FROM shops"
        )
        db.execute("DROP TABLE shops")
        db.execute("ALTER TABLE shops_platform RENAME TO shops")
        if sequence:
            db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='shops'", (sequence["seq"],))
