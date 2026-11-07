"""
SQLite 业务库连接管理。

为什么单独封装：
- 订单、工单、物流是"事实权威"，必须用同一个连接管理器统一打开/关闭。
- 写操作（创建工单）需要事务与唯一约束保证幂等，不能让路由层直接拼 SQL。
- 连接生命周期由上下文管理器控制，避免连接泄漏。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from backend.config import SQLITE_DB_PATH


def get_connection() -> sqlite3.Connection:
    """打开业务库连接，开启外键约束，行工厂返回 Row（便于按列名取值）。"""
    con = sqlite3.connect(SQLITE_DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    """
    业务库会话上下文管理器。

    用法：
        with db_session() as con:
            row = con.execute("SELECT * FROM orders WHERE order_id=?", (oid,)).fetchone()

    退出时自动提交（写操作）并关闭连接；异常时自动回滚。
    """
    con = get_connection()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# 为了让其他模块可以直接 import 使用，也提供一个模块级便捷函数
def fetch_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """执行查询并返回单行（常用于按 ID 查询）。"""
    with db_session() as con:
        return con.execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """执行查询并返回所有行。"""
    with db_session() as con:
        return con.execute(sql, params).fetchall()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """执行写操作并返回游标（用于获取 lastrowid 等）。"""
    with db_session() as con:
        return con.execute(sql, params)
