from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from myquant.config import Settings


SCHEMAS: tuple[str, ...] = ("raw", "market", "research", "portfolio", "simulation", "ops")


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schemas(engine: Engine, schemas: Iterable[str] = SCHEMAS) -> None:
    with engine.begin() as connection:
        for schema in schemas:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    Base.metadata.create_all(engine)
    # This project predates a migration framework.  Keep the additive schema
    # upgrade explicit and idempotent so existing local research databases can
    # record benchmark-weight provenance without being recreated.
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE market.index_constituent "
            "ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'unknown'"
        ))
        connection.execute(text(
            "ALTER TABLE market.index_constituent "
            "ADD COLUMN IF NOT EXISTS methodology VARCHAR(64) NOT NULL DEFAULT 'unknown'"
        ))
