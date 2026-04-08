"""
Local daemon state database.

Persists across restarts:
  - Which shares this peer is participating in
  - Current torrent info-hash per share
  - File index per share (path → mtime/size for change detection)
  - Sync state per share

SQLite only — this is local to the daemon, never shared.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, ForeignKey,
    Integer, String, UniqueConstraint, create_engine, Text, text,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


class ShareState(PyEnum):
    SYNCING  = "syncing"
    SEEDING  = "seeding"
    PAUSED   = "paused"
    ERROR    = "error"


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class LocalShare(Base):
    """A share this daemon participates in."""
    __tablename__ = "local_shares"

    share_id    = Column(String, primary_key=True)
    name        = Column(String, nullable=False, default="")
    local_path  = Column(String, nullable=False)
    permission  = Column(String, nullable=False, default="rw")
    is_owner    = Column(Boolean, nullable=False, default=False)
    state       = Column(Enum(ShareState), nullable=False,
                         default=ShareState.SYNCING)
    info_hash      = Column(String, nullable=True)   # current libtorrent info-hash
    last_error     = Column(String, nullable=True)
    upload_limit   = Column(Integer, nullable=False, default=0)   # bytes/sec, 0 = unlimited
    download_limit = Column(Integer, nullable=False, default=0)
    added_at    = Column(DateTime(timezone=True), default=utcnow)
    updated_at  = Column(DateTime(timezone=True), default=utcnow,
                         onupdate=utcnow)

    files = relationship("LocalFile", back_populates="share",
                         cascade="all, delete-orphan")


class ShareKeypair(Base):
    """
    Ed25519 keypair for shares this peer created.
    Only the creating peer holds the share private key.
    Stored as raw 32-byte seed, base64-encoded.
    The db file itself is 600 — this provides protection at rest.
    """
    __tablename__ = "share_keypairs"

    share_id   = Column(String, primary_key=True)
    # base64-encoded 32-byte Ed25519 seed
    seed_b64   = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class LocalFile(Base):
    """
    Tracks the last-seen state of each file in a share.
    Used to detect changes without a full re-hash.
    """
    __tablename__ = "local_files"
    __table_args__ = (UniqueConstraint("share_id", "rel_path"),)

    id        = Column(Integer, primary_key=True, autoincrement=True)
    share_id  = Column(String, ForeignKey("local_shares.share_id"), nullable=False)
    rel_path  = Column(String, nullable=False)   # relative to share local_path
    size      = Column(Integer, nullable=False, default=0)
    mtime_ns  = Column(Integer, nullable=False, default=0)  # nanoseconds
    sha256    = Column(String, nullable=True)     # populated after hash
    seen_at   = Column(DateTime(timezone=True), default=utcnow)

    share = relationship("LocalShare", back_populates="files")


class KnownPeer(Base):
    """
    Cache of peers seen online for each share.
    Populated from WatchSharePeers events; used to seed libtorrent
    with tracker URLs on startup before the stream reconnects.
    """
    __tablename__ = "known_peers"
    __table_args__ = (UniqueConstraint("share_id", "peer_id"),)

    id        = Column(Integer, primary_key=True, autoincrement=True)
    share_id  = Column(String, nullable=False)
    peer_id   = Column(String, nullable=False)
    # JSON list of {"host": str, "port": int, "is_lan": bool}
    addresses = Column(Text, nullable=False, default="[]")
    online    = Column(Boolean, nullable=False, default=False)
    last_seen = Column(DateTime(timezone=True), nullable=True)


# ── Session factory ───────────────────────────────────────────────────────────

def make_state_db(data_dir: str):
    """
    Create (or open) the local state database in data_dir.
    Returns a session factory.
    """
    import os
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "daemon.db")
    engine  = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    _migrate(engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate(engine):
    """Add columns introduced after initial schema (idempotent)."""
    migrations = [
        ("local_shares", "upload_limit",   "INTEGER NOT NULL DEFAULT 0"),
        ("local_shares", "download_limit", "INTEGER NOT NULL DEFAULT 0"),
    ]
    with engine.connect() as conn:
        for table, col, typedef in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}"))
                conn.commit()
            except Exception:
                pass  # column already exists


# ── Repository helpers ────────────────────────────────────────────────────────

class StateDB:
    """Thin wrapper around the session factory with typed helpers."""

    def __init__(self, session_factory):
        self._sf = session_factory

    # ── Shares ────────────────────────────────────────────────────────────────

    def get_share(self, share_id: str) -> LocalShare | None:
        with self._sf() as s:
            return s.query(LocalShare).filter_by(share_id=share_id).first()

    def list_shares(self) -> list[LocalShare]:
        with self._sf() as s:
            return s.query(LocalShare).all()

    def add_share(self, share_id: str, name: str, local_path: str,
                  permission: str = "rw", is_owner: bool = False) -> LocalShare:
        with self._sf() as s:
            existing = s.query(LocalShare).filter_by(share_id=share_id).first()
            if existing:
                existing.local_path = local_path
                existing.permission = permission
                existing.name       = name
                existing.is_owner   = is_owner or existing.is_owner
                s.commit()
                return existing
            share = LocalShare(
                share_id   = share_id,
                name       = name,
                local_path = local_path,
                permission = permission,
                is_owner   = is_owner,
                state      = ShareState.SYNCING,
            )
            s.add(share)
            s.commit()
            s.refresh(share)
            return share

    def remove_share(self, share_id: str):
        with self._sf() as s:
            share = s.query(LocalShare).filter_by(share_id=share_id).first()
            if share:
                s.delete(share)
                s.commit()

    def set_share_rate_limits(self, share_id: str,
                              upload_limit: int, download_limit: int):
        with self._sf() as s:
            share = s.query(LocalShare).filter_by(share_id=share_id).first()
            if share:
                share.upload_limit   = upload_limit
                share.download_limit = download_limit
                s.commit()

    def set_share_state(self, share_id: str, state: ShareState,
                        error: str | None = None, info_hash: str | None = None):
        with self._sf() as s:
            share = s.query(LocalShare).filter_by(share_id=share_id).first()
            if share:
                share.state      = state
                share.last_error = error
                if info_hash is not None:
                    share.info_hash = info_hash
                s.commit()

    # ── Files ─────────────────────────────────────────────────────────────────

    def upsert_file(self, share_id: str, rel_path: str,
                    size: int, mtime_ns: int, sha256: str | None = None):
        with self._sf() as s:
            f = (s.query(LocalFile)
                  .filter_by(share_id=share_id, rel_path=rel_path)
                  .first())
            if f:
                f.size     = size
                f.mtime_ns = mtime_ns
                if sha256:
                    f.sha256 = sha256
            else:
                s.add(LocalFile(share_id=share_id, rel_path=rel_path,
                                size=size, mtime_ns=mtime_ns, sha256=sha256))
            s.commit()

    def delete_file(self, share_id: str, rel_path: str):
        with self._sf() as s:
            f = (s.query(LocalFile)
                  .filter_by(share_id=share_id, rel_path=rel_path)
                  .first())
            if f:
                s.delete(f)
                s.commit()

    def get_files(self, share_id: str) -> list[LocalFile]:
        with self._sf() as s:
            return s.query(LocalFile).filter_by(share_id=share_id).all()

    # ── Known peers ───────────────────────────────────────────────────────────

    def upsert_peer(self, share_id: str, peer_id: str,
                    addresses: list[dict], online: bool):
        with self._sf() as s:
            p = (s.query(KnownPeer)
                  .filter_by(share_id=share_id, peer_id=peer_id)
                  .first())
            if p:
                p.addresses = json.dumps(addresses)
                p.online    = online
                p.last_seen = utcnow() if online else p.last_seen
            else:
                s.add(KnownPeer(
                    share_id  = share_id,
                    peer_id   = peer_id,
                    addresses = json.dumps(addresses),
                    online    = online,
                    last_seen = utcnow() if online else None,
                ))
            s.commit()

    def get_online_peers(self, share_id: str) -> list[KnownPeer]:
        with self._sf() as s:
            return (s.query(KnownPeer)
                     .filter_by(share_id=share_id, online=True)
                     .all())


    # ── Share keypairs (owned shares only) ────────────────────────────────────

    def save_share_keypair(self, share_id: str, seed: bytes):
        """Persist the Ed25519 seed for a share this peer created."""
        import base64
        with self._sf() as s:
            existing = s.query(ShareKeypair).filter_by(share_id=share_id).first()
            if existing:
                existing.seed_b64 = base64.b64encode(seed).decode()
            else:
                s.add(ShareKeypair(
                    share_id = share_id,
                    seed_b64 = base64.b64encode(seed).decode(),
                ))
            s.commit()

    def get_share_keypair(self, share_id: str) -> bytes | None:
        """Return raw 32-byte Ed25519 seed for a share, or None if not owner."""
        import base64
        with self._sf() as s:
            kp = s.query(ShareKeypair).filter_by(share_id=share_id).first()
            if kp:
                return base64.b64decode(kp.seed_b64)
            return None

    def list_all_known_peers(self, share_id: str) -> list:
        """Return all known peers for a share (online and offline)."""
        with self._sf() as s:
            return s.query(KnownPeer).filter_by(share_id=share_id).all()
