"""
SQLAlchemy models for the registry.

Using SQLite by default (portable, no external dependency).
Swap DATABASE_URL in config.py for PostgreSQL in production.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

import enum as pyenum


class PermissionEnum(pyenum.Enum):
    READ_WRITE = "read_write"
    READ_ONLY  = "read_only"
    ENCRYPTED  = "encrypted"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PeerModel(Base):
    __tablename__ = "peers"

    peer_id    = Column(String, primary_key=True)   # Ed25519 pubkey, base58
    name       = Column(String, nullable=False)
    token_hash = Column(String, nullable=False)      # bcrypt hash of bearer token
    created_at = Column(DateTime(timezone=True), default=utcnow)

    share_memberships = relationship("SharePeerModel", back_populates="peer",
                                     cascade="all, delete-orphan")
    announcements     = relationship("AnnouncementModel", back_populates="peer",
                                     cascade="all, delete-orphan")


class ShareModel(Base):
    __tablename__ = "shares"

    share_id           = Column(String, primary_key=True)   # Ed25519 pubkey, base58
    name               = Column(String, nullable=False)
    owner_id           = Column(String, ForeignKey("peers.peer_id"), nullable=False)
    created_at         = Column(DateTime(timezone=True), default=utcnow)
    upload_limit_bps   = Column(Integer, nullable=False, default=0, server_default="0")
    download_limit_bps = Column(Integer, nullable=False, default=0, server_default="0")

    owner   = relationship("PeerModel", foreign_keys=[owner_id])
    members = relationship("SharePeerModel", back_populates="share",
                           cascade="all, delete-orphan")


class SharePeerModel(Base):
    __tablename__ = "share_peers"
    __table_args__ = (UniqueConstraint("share_id", "peer_id"),)

    id         = Column(Integer, primary_key=True, autoincrement=True)
    share_id   = Column(String, ForeignKey("shares.share_id"), nullable=False)
    peer_id    = Column(String, ForeignKey("peers.peer_id"),  nullable=False)
    permission = Column(Enum(PermissionEnum), nullable=False,
                        default=PermissionEnum.READ_WRITE)
    added_at   = Column(DateTime(timezone=True), default=utcnow)

    share = relationship("ShareModel", back_populates="members")
    peer  = relationship("PeerModel",  back_populates="share_memberships")


class AnnouncementModel(Base):
    """
    Ephemeral. One row per (peer, share) — upserted on each announce.
    Peers are considered online while expires_at > utcnow().
    """
    __tablename__ = "announcements"
    __table_args__ = (UniqueConstraint("share_id", "peer_id"),)

    id              = Column(Integer, primary_key=True, autoincrement=True)
    share_id        = Column(String, ForeignKey("shares.share_id"), nullable=False)
    peer_id         = Column(String, ForeignKey("peers.peer_id"),  nullable=False)
    # Stored as JSON string: '[{"host":"1.2.3.4","port":55000,"is_lan":true}, ...]'
    internal_addrs  = Column(String, nullable=False, default="[]")
    external_host   = Column(String, nullable=True)
    external_port   = Column(Integer, nullable=True)
    info_hash       = Column(String, nullable=True)   # hex libtorrent info-hash
    announced_at    = Column(DateTime(timezone=True), default=utcnow)
    expires_at      = Column(DateTime(timezone=True), nullable=False)

    share = relationship("ShareModel")
    peer  = relationship("PeerModel", back_populates="announcements")


def make_engine(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


def make_session_factory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def create_tables(engine):
    Base.metadata.create_all(bind=engine)
