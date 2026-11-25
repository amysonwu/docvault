# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 DocVault Contributors
# Author: Amyson Wu
"""数据库引擎与会话（GreatSQL / MySQL 8）"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_size=10,
    max_overflow=20,
    echo=False,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from . import models  # noqa: F401 确保表注册
    Base.metadata.create_all(engine)
    _ensure_fulltext()


def _ensure_fulltext():
    """中文全文检索依赖 ngram FULLTEXT 索引；create_all 不建索引，这里幂等补齐。

    单字检索走 LIKE 兜底，不受此影响；长词检索必须命中该索引，否则接口 500。
    """
    ddl = ("CREATE FULLTEXT INDEX ft_documents_ngram ON documents "
           "(name, content_text) WITH PARSER ngram")
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "AND TABLE_NAME = 'documents' AND INDEX_NAME = 'ft_documents_ngram'"
        )).scalar()
        if not exists:
            conn.execute(text(ddl))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
