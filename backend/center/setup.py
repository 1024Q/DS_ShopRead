"""Interactive PostgreSQL bootstrap; never prints passwords or connection URLs."""

import argparse
import getpass
import json
import secrets
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from .database import Database


def initialize(host, port, admin, password, directory):
    directory = Path(directory)
    config = directory / "center.local.json"
    if config.exists():
        raise ValueError("中心配置已存在，拒绝覆盖。请使用现有配置启动中心。")
    role = "ecmp"
    dbname = "ecmp"
    app_password = secrets.token_urlsafe(36)
    with psycopg.connect(
        host=host, port=port, user=admin, password=password, dbname="postgres", connect_timeout=10, autocommit=True
    ) as connection:
        with connection.cursor() as cursor:
            if cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                raise ValueError("数据库角色 ecmp 已存在；不会修改其密码。请配置现有数据库连接。")
            if cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (dbname,)).fetchone():
                raise ValueError("数据库 ecmp 已存在；不会覆盖或删除。请配置现有数据库连接。")
            # Reserve the configuration first: on a later database failure credentials remain recoverable.
            url = URL.create(
                "postgresql+psycopg", username=role, password=app_password, host=host, port=port, database=dbname
            ).render_as_string(hide_password=False)
            values = {"ECMP_DATABASE_URL": url, "ECMP_CENTER_ADMIN_TOKEN": secrets.token_urlsafe(48)}
            with config.open("x", encoding="utf-8") as output:
                json.dump(values, output, indent=2)
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(app_password)
                )
            )
            cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(role)))
    database = Database(url)
    database.engine.dispose()
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--admin", default="postgres")
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    password = getpass.getpass("PostgreSQL postgres 管理员密码（不保存）: ")
    try:
        path = initialize(args.host, args.port, args.admin, password, args.directory)
    except (ValueError, psycopg.Error, SQLAlchemyError) as error:
        # Avoid logging SQLAlchemy URLs, server diagnostics containing credentials, or raw SQL.
        print(
            str(error)
            if isinstance(error, ValueError)
            else "数据库初始化失败。请检查 PostgreSQL 服务、端口和管理员密码；若生成了配置，请保留以便恢复。"
        )
        raise SystemExit(1)
    print(f"数据库与中心表已初始化。配置保存在受限目录：{path}。不要将此文件发到聊天或提交到 Git。")


if __name__ == "__main__":
    main()
