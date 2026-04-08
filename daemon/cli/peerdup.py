"""
peerdup CLI — talks to the daemon over the control Unix socket.

Usage:
    peerdup [--socket PATH] <command> [args]

Commands:
    identity                              Show this peer's identity
    share list                            List active shares
    share info <share_id>                 Show metadata for a single share
    share peers <share_id>                Show peer roster for a share
    share create <name> <path>            Create a new share (generates share_id)
    share add <share_id> <path>           Join an existing share
    share remove <share_id>               Remove a share (optionally --delete-files)
    share set-limit <share_id>                       Set per-share rate limits (--up / --down)
    share pause <share_id>                           Pause syncing a share
    share resume <share_id>                          Resume a paused share
    share grant <share_id> <peer_id>                 Grant a peer access to your share
    share revoke <share_id> <peer_id>                Revoke a peer's access
    share set-conflict <share_id> <strategy>         Set conflict resolution strategy
    share conflicts <share_id>                       List pending conflicts (ask strategy)
    share resolve <conflict_id> <keep-local|keep-remote>  Resolve a conflict
    status                                           Show current sync status
    watch                                            Stream live status events (Ctrl-C to stop)
"""

from __future__ import annotations

import argparse
import json
import sys

import grpc


import os

def _default_socket() -> str:
    """
    Discover the control socket without requiring any configuration.

    Resolution order:
      1. PEERDUP_SOCKET env var (explicit override)
      2. $XDG_RUNTIME_DIR/peerdup/control.sock  — user install (exists → use it)
      3. /run/peerdup/control.sock              — system install (exists → use it)
      4. $XDG_RUNTIME_DIR path as best-guess fallback (will fail with clear error)
    """
    if "PEERDUP_SOCKET" in os.environ:
        return os.environ["PEERDUP_SOCKET"]

    candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(os.path.join(xdg, "peerdup", "control.sock"))
    candidates.append("/run/peerdup/control.sock")

    for path in candidates:
        if os.path.exists(path):
            return path

    # Nothing found — return the XDG path so the error message is meaningful.
    return candidates[0] if candidates else "/run/peerdup/control.sock"

DEFAULT_SOCKET = _default_socket()


def _channel(socket_path: str) -> grpc.Channel:
    return grpc.insecure_channel(f"unix://{socket_path}")


def _stub(socket_path: str):
    from daemon import control_pb2_grpc  # type: ignore
    return control_pb2_grpc.ControlServiceStub(_channel(socket_path))


def _resolve_share(stub, name_or_id: str) -> str:
    """Resolve a share name or share_id to a share_id.

    Tries exact name match first, then prefix match, then treats the
    argument as a literal share_id.
    """
    from daemon import control_pb2  # type: ignore
    try:
        resp = stub.ListShares(control_pb2.ListSharesRequest())
    except grpc.RpcError:
        return name_or_id  # can't resolve, pass through and let the RPC fail

    # Exact name match
    for s in resp.shares:
        if s.name == name_or_id:
            return s.share_id

    # share_id prefix match (user typed first few chars)
    matches = [s for s in resp.shares if s.share_id.startswith(name_or_id)]
    if len(matches) == 1:
        return matches[0].share_id
    if len(matches) > 1:
        names = ", ".join(s.name for s in matches)
        print(f"Error: ambiguous share reference '{name_or_id}' matches: {names}",
              file=sys.stderr)
        sys.exit(1)

    # Fall through — treat as full share_id
    return name_or_id


def cmd_identity(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    try:
        resp = stub.ShowIdentity(control_pb2.IdentityRequest())
        print(f"peer_id : {resp.peer_id}")
        print(f"name    : {resp.name or '(not set)'}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_list(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    try:
        resp = stub.ListShares(control_pb2.ListSharesRequest())
        if getattr(args, 'json', False):
            print(json.dumps({'shares': [_share_to_dict(s) for s in resp.shares]}),
                  flush=True)
            return
        if not resp.shares:
            print("No active shares.")
            return
        _print_shares(resp.shares)
    except grpc.RpcError as e:
        _err(e)


def cmd_share_info(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        r = stub.GetShareInfo(control_pb2.GetShareInfoRequest(share_id=share_id))
    except grpc.RpcError as e:
        _err(e)
        return

    sid_full  = r.share_id
    sid_short = sid_full[:16] + ".." if len(sid_full) > 18 else sid_full
    owner_tag = " (you)" if r.is_owner else ""

    print(f"Share: {r.name}  ({sid_short})")
    print("─" * 60)
    print(f"  share_id   : {sid_full}")
    print(f"  name       : {r.name or '(not set)'}")
    print(f"  local_path : {r.local_path}")
    print(f"  state      : {r.state}")
    print(f"  permission : {r.permission}")
    print(f"  owner      : {r.owner_id[:16] + '..' if len(r.owner_id) > 18 else r.owner_id}{owner_tag}")
    if r.created_at:
        print(f"  created_at : {_format_last_seen(r.created_at)} ({r.created_at[:19]})")
    print(f"  info_hash  : {r.info_hash or '(none)'}")
    print(f"  progress   : {_human(r.bytes_done)} / {_human(r.bytes_total)}")
    print(f"  peers      : {r.peers_online} online / {r.total_peers} total")
    up_str   = f"{_human(r.upload_limit)}/s"   if r.upload_limit   else "unlimited"
    down_str = f"{_human(r.download_limit)}/s" if r.download_limit else "unlimited"
    print(f"  rate limit : ↑ {up_str}  ↓ {down_str}")
    print(f"  conflicts  : {r.conflict_strategy}", end="")
    if r.pending_conflicts:
        print(f"  ({r.pending_conflicts} pending - run: peerdup share conflicts {r.share_id[:16]}..)")
    else:
        print()
    if r.last_error:
        print(f"  error      : {r.last_error}")
    print("─" * 60)


def cmd_share_peers(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        resp = stub.ListSharePeers(control_pb2.ListSharePeersRequest(
            share_id = share_id,
        ))
    except grpc.RpcError as e:
        _err(e)
        return

    sid_short = args.share_id[:16] + ".."
    print(f"Share: {resp.share_name}  ({sid_short})")
    print("─" * 72)

    if not resp.peers:
        print("  No peers found.")
    else:
        col = "{:<3} {:<10} {:<12} {:<10} {:<4} {:<20} {}"
        print(col.format("", "PEER_ID", "NAME", "PERMISSION",
                         "STAT", "LAST SEEN", "ADDRESSES"))
        print("  " + "─" * 68)
        for p in resp.peers:
            marker   = "▶" if p.is_self else " "
            pid      = p.peer_id[:8] + ".."
            status   = "● online" if p.online else "○ offline"
            last     = _format_last_seen(p.last_seen) if p.last_seen else "—"
            addrs    = ", ".join(p.addresses) if p.addresses else "—"
            perm     = p.permission
            if p.is_owner:
                perm += " (owner)"
            print(col.format(marker, pid, p.name[:12], perm,
                             status[:4], last[:20], addrs))
            # Show transfer rates if active.
            if p.online and (p.download_rate or p.upload_rate):
                print(f"     ↓ {_human(p.download_rate)}/s  "
                      f"↑ {_human(p.upload_rate)}/s")

    print("─" * 72)
    print(f"{resp.total_granted} peer(s) with access, "
          f"{resp.total_online} online")


def cmd_share_create(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)

    import_key_hex = ""
    if getattr(args, "import_key", None):
        try:
            with open(args.import_key, "rb") as f:
                seed = f.read()
            if len(seed) != 32:
                print(f"Error: key file must be exactly 32 bytes (got {len(seed)})",
                      file=sys.stderr)
                sys.exit(1)
            import_key_hex = seed.hex()
        except OSError as e:
            print(f"Error reading key file: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        resp = stub.CreateShare(control_pb2.CreateShareRequest(
            name              = args.name,
            local_path        = args.path,
            permission        = args.permission,
            import_key_hex    = import_key_hex,
            conflict_strategy = args.conflict,
            local_only        = args.local,
        ))
        action = "imported" if import_key_hex else "created"
        mode   = "local" if args.local else "registry"
        print(f"Share {action} successfully! (mode: {mode})")
        print(f"share_id    : {resp.share_id}")
        print(f"fingerprint : {resp.share_id[:8]}  (first 8 chars — verify with peers)")
        print(f"")
        if args.local:
            print(f"Local-only share - no registry required. Give this share_id to peers")
            print(f"on the same LAN, then have them run:")
            print(f"  peerdup share add --local {resp.share_id} <their-local-path>")
        else:
            print(f"Give this share_id to peers, then have them run:")
            print(f"  peerdup share grant {resp.share_id} <their-peer-id>")
            print(f"  peerdup share add   {resp.share_id} <their-local-path>")
        print(f"")
        _print_shares([resp.share])
    except grpc.RpcError as e:
        _err(e)


def cmd_share_grant(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        resp = stub.GrantAccess(control_pb2.GrantAccessRequest(
            share_id   = share_id,
            peer_id    = args.peer_id,
            permission = args.permission,
        ))
        print(f"Access granted:")
        print(f"  share_id   : {resp.share_id}")
        print(f"  peer_id    : {resp.peer_id}")
        print(f"  permission : {resp.permission}")
        print(f"")
        print(f"The peer can now join with:")
        print(f"  peerdup share add {resp.share_id} <their-local-path>")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_revoke(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        stub.RevokeAccess(control_pb2.RevokeAccessRequest(
            share_id = share_id,
            peer_id  = args.peer_id,
        ))
        print(f"Access revoked for peer {args.peer_id} on share {args.share_id}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_add(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        resp = stub.AddShare(control_pb2.AddShareRequest(
            share_id          = share_id,
            local_path        = args.path,
            permission        = args.permission,
            conflict_strategy = args.conflict,
            local_only        = args.local,
        ))
        print(f"Added share:")
        _print_shares([resp.share])
    except grpc.RpcError as e:
        _err(e)


def cmd_share_remove(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        stub.RemoveShare(control_pb2.RemoveShareRequest(
            share_id     = share_id,
            delete_files = args.delete_files,
        ))
        print(f"Removed share {args.share_id}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_set_limit(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        up   = _parse_rate(args.up)   if args.up   else 0
        down = _parse_rate(args.down) if args.down else 0
        resp = stub.SetShareRateLimit(control_pb2.SetShareRateLimitRequest(
            share_id       = share_id,
            upload_limit   = up,
            download_limit = down,
        ))
        up_str   = f"{_human(resp.upload_limit)}/s"   if resp.upload_limit   else "unlimited"
        down_str = f"{_human(resp.download_limit)}/s" if resp.download_limit else "unlimited"
        print(f"Rate limits set for {share_id[:16]}..")
        print(f"  upload   : {up_str}")
        print(f"  download : {down_str}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_pause(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        stub.PauseShare(control_pb2.PauseShareRequest(share_id=share_id))
        print(f"Paused share {share_id}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_resume(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        stub.ResumeShare(control_pb2.ResumeShareRequest(share_id=share_id))
        print(f"Resumed share {share_id}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_set_conflict(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        resp = stub.SetConflictStrategy(control_pb2.SetConflictStrategyRequest(
            share_id          = share_id,
            conflict_strategy = args.strategy,
        ))
        print(f"Conflict strategy set for {share_id[:16]}..")
        print(f"  strategy : {resp.conflict_strategy}")
    except grpc.RpcError as e:
        _err(e)


def cmd_share_conflicts(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    share_id = _resolve_share(stub, args.share_id)
    try:
        resp = stub.ListConflicts(control_pb2.ListConflictsRequest(
            share_id = share_id,
        ))
    except grpc.RpcError as e:
        _err(e)
        return

    if not resp.conflicts:
        print("No pending conflicts.")
        return

    print(f"Pending conflicts for share {share_id[:16]}..")
    print("-" * 72)
    for c in resp.conflicts:
        detected = _format_last_seen(c.detected_at)
        print(f"  ID       : {c.conflict_id}")
        print(f"  Peer     : {c.remote_peer_id[:16]}..")
        print(f"  Remote   : {c.remote_info_hash[:16]}..")
        print(f"  Local    : {(c.local_info_hash or '(none)')[:16]}..")
        print(f"  Detected : {detected}")
        print(f"  Resolve  : peerdup share resolve {c.conflict_id} <keep-local|keep-remote>")
        print()


def cmd_share_resolve(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    try:
        resp = stub.ResolveConflict(control_pb2.ResolveConflictRequest(
            conflict_id = args.conflict_id,
            resolution  = args.resolution,
        ))
        print(f"Conflict {resp.conflict_id} resolved: {resp.resolution}")
    except grpc.RpcError as e:
        _err(e)


def cmd_status(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    try:
        resp = stub.Status(control_pb2.StatusRequest())
        print(resp.message)
        if resp.HasField("share"):
            _print_shares([resp.share])
    except grpc.RpcError as e:
        _err(e)


def cmd_watch(args):
    from daemon import control_pb2  # type: ignore
    stub = _stub(args.socket)
    use_json = getattr(args, 'json', False)
    if not use_json:
        print("Watching for status events (Ctrl-C to stop)...")
    try:
        for event in stub.WatchStatus(control_pb2.StatusRequest()):
            if use_json:
                _print_event_json(event)
            else:
                _print_event(event)
    except KeyboardInterrupt:
        pass
    except grpc.RpcError as e:
        _err(e)


# ── JSON helpers (machine-readable output) ────────────────────────────────────

def _share_to_dict(s) -> dict:
    return {
        'share_id':      s.share_id,
        'name':          s.name,
        'local_path':    s.local_path,
        'state':         s.state,
        'mode':          s.mode or 'registry',
        'bytes_done':    s.bytes_done,
        'bytes_total':   s.bytes_total,
        'lt_peers':      s.lt_peers,
        'peers_online':  s.peers_online,
        'upload_rate':   s.upload_rate,
        'download_rate': s.download_rate,
        'last_error':    s.last_error,
        'conflict_strategy': s.conflict_strategy,
    }


def _print_event_json(event):
    from daemon import control_pb2  # type: ignore
    type_map = {
        control_pb2.StatusEvent.EVENT_TYPE_SHARE_UPDATED: 'share_updated',
        control_pb2.StatusEvent.EVENT_TYPE_PEER_EVENT:    'peer_event',
        control_pb2.StatusEvent.EVENT_TYPE_SYNC_PROGRESS: 'sync_progress',
        control_pb2.StatusEvent.EVENT_TYPE_ERROR:         'error',
        control_pb2.StatusEvent.EVENT_TYPE_CONFLICT:      'conflict',
    }
    obj = {
        'type':    type_map.get(event.type, 'unknown'),
        'message': event.message,
    }
    if event.conflict_id:
        obj['conflict_id'] = event.conflict_id
    print(json.dumps(obj), flush=True)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _print_peers(resp):
    """Pretty-print a ListSharePeersResponse."""
    sid   = resp.share_id
    short = sid[:18] + ".." if len(sid) > 20 else sid
    print(f"Share: {resp.share_name}  ({short})")
    print("─" * 72)

    if not resp.peers:
        print("  No peers.")
    else:
        col = "{:<10} {:<18} {:<12} {:<11} {:<8} {:<16} {}"
        print(col.format("", "PEER_ID", "NAME", "PERMISSION",
                         "STATUS", "LAST SEEN", "ADDRESSES"))
        print("  " + "─" * 68)

        for p in resp.peers:
            pid    = p.peer_id[:16] + ".." if len(p.peer_id) > 18 else p.peer_id
            name   = (p.name or "—")[:12]
            perm   = p.permission
            status = "● online" if p.online else "○ offline"
            seen   = _friendly_time(p.last_seen) if p.last_seen else "never"
            addrs  = ", ".join(p.addresses) if p.addresses else "—"

            badge = ""
            if p.is_self:  badge += "[me]"
            if p.is_owner: badge += "[owner]"

            print(col.format(badge, pid, name, perm, status, seen, addrs))

            if p.download_rate or p.upload_rate:
                print(f"           ↓ {_human(p.download_rate)}/s  "
                      f"↑ {_human(p.upload_rate)}/s")

    print("─" * 72)
    print(f"{resp.total_granted} peer(s) granted access, "
          f"{resp.total_online} online")


def _friendly_time(iso: str) -> str:
    """Convert ISO-8601 timestamp to a human-friendly relative string."""
    if not iso:
        return "never"
    try:
        from datetime import datetime, timezone
        dt  = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = int((now - dt).total_seconds())
        if diff < 10:    return "just now"
        if diff < 60:    return f"{diff}s ago"
        if diff < 3600:  return f"{diff // 60}m ago"
        if diff < 86400: return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return iso[:16]


def _print_shares(shares):
    col = "{:<20} {:<12} {:<8} {:<8} {:>8} {:>8} {:>8}  {}"
    print(col.format("SHARE_ID", "NAME", "MODE", "STATE",
                     "DONE", "TOTAL", "PEERS", "PATH"))
    print("  (PEERS = active/announced)")
    print("-" * 90)
    for s in shares:
        done  = _human(s.bytes_done)
        total = _human(s.bytes_total)
        sid   = s.share_id[:18] + ".." if len(s.share_id) > 20 else s.share_id
        mode  = s.mode or "registry"
        peers = f"{s.lt_peers}/{s.peers_online}"
        print(col.format(sid, s.name[:12], mode, s.state,
                         done, total, peers, s.local_path))
        if s.upload_rate or s.download_rate:
            print(f"       ↑ {_human(s.upload_rate)}/s  ↓ {_human(s.download_rate)}/s")
        if s.last_error:
            print(f"  ERROR: {s.last_error}")


def _print_event(event):
    from daemon import control_pb2  # type: ignore
    type_names = {
        control_pb2.StatusEvent.EVENT_TYPE_SHARE_UPDATED:  "SHARE",
        control_pb2.StatusEvent.EVENT_TYPE_PEER_EVENT:     "PEER",
        control_pb2.StatusEvent.EVENT_TYPE_SYNC_PROGRESS:  "PROGRESS",
        control_pb2.StatusEvent.EVENT_TYPE_ERROR:          "ERROR",
        control_pb2.StatusEvent.EVENT_TYPE_CONFLICT:       "CONFLICT",
    }
    name = type_names.get(event.type, "?")
    if event.type == control_pb2.StatusEvent.EVENT_TYPE_CONFLICT and event.conflict_id:
        print(f"[{name}] id={event.conflict_id} {event.message}")
    else:
        print(f"[{name}] {event.message}")


def _format_last_seen(iso: str) -> str:
    """Convert ISO timestamp to a human-friendly relative string."""
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        dt  = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((now - dt).total_seconds())
        if secs < 10:
            return "just now"
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"
    except Exception:
        return iso[:16]


def _parse_rate(s: str) -> int:
    """Parse a human-readable rate like '10M', '512K', '1G' to bytes/sec."""
    s = s.strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}
    for suffix, mult in sorted(units.items(), key=lambda x: -x[1]):
        if s.endswith(suffix):
            return int(float(s[:-1]) * mult)
    return int(s)  # bare number = bytes/sec


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}P"


def _err(e: grpc.RpcError):
    print(f"Error: {e.code().name}: {e.details()}", file=sys.stderr)
    sys.exit(1)


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="peerdup",
        description="peerdup CLI — control the local sync daemon",
    )
    p.add_argument("--socket", default=DEFAULT_SOCKET,
                   help=f"Control socket path (default: {DEFAULT_SOCKET})")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("identity", help="Show peer identity")

    # share subcommands
    share_p = sub.add_parser("share", help="Manage shares")
    share_sub = share_p.add_subparsers(dest="share_command", required=True)

    list_p = share_sub.add_parser("list", help="List active shares")
    list_p.add_argument("--json", action="store_true", default=False,
                        help="Output machine-readable JSON")

    info_p = share_sub.add_parser("info", help="Show metadata for a single share")
    info_p.add_argument("share_id")

    peers_p = share_sub.add_parser("peers", help="Show peers for a share")
    peers_p.add_argument("share_id")

    create_p = share_sub.add_parser("create", help="Create a new share")
    create_p.add_argument("name",  help="Human-readable share name")
    create_p.add_argument("path",  help="Local directory to sync")
    create_p.add_argument("--permission", default="rw",
                          choices=["rw", "ro", "encrypted"],
                          help="Your own permission level (default: rw)")
    create_p.add_argument("--import-key", metavar="PATH",
                          help="Import an existing 32-byte Ed25519 seed file "
                               "instead of generating a new keypair")
    create_p.add_argument("--conflict", default="last_write_wins",
                          choices=["last_write_wins", "rename_conflict", "ask"],
                          dest="conflict",
                          help="Conflict resolution strategy (default: last_write_wins)")
    create_p.add_argument("--local", action="store_true", default=False,
                          help="Local-only share: skip registry, use LAN discovery only")

    add_p = share_sub.add_parser("add", help="Join an existing share")
    add_p.add_argument("share_id",   help="Share ID to join")
    add_p.add_argument("path",       help="Local directory to sync")
    add_p.add_argument("--permission", default="rw",
                       choices=["rw", "ro", "encrypted"])
    add_p.add_argument("--conflict", default="last_write_wins",
                       choices=["last_write_wins", "rename_conflict", "ask"],
                       dest="conflict",
                       help="Conflict resolution strategy (default: last_write_wins)")
    add_p.add_argument("--local", action="store_true", default=False,
                       help="Local-only share: skip registry validation, use LAN discovery only")

    rm_p = share_sub.add_parser("remove", help="Remove a share")
    rm_p.add_argument("share_id")
    rm_p.add_argument("--delete-files", action="store_true",
                      help="Also delete local files")

    grant_p = share_sub.add_parser("grant", help="Grant a peer access to your share")
    grant_p.add_argument("share_id")
    grant_p.add_argument("peer_id", help="Peer's base58 pubkey (from: peerdup identity)")
    grant_p.add_argument("--permission", default="rw",
                         choices=["rw", "ro", "encrypted"])

    revoke_p = share_sub.add_parser("revoke", help="Revoke a peer's access")
    revoke_p.add_argument("share_id")
    revoke_p.add_argument("peer_id")

    limit_p = share_sub.add_parser("set-limit", help="Set per-share rate limits")
    limit_p.add_argument("share_id")
    limit_p.add_argument("--up",   metavar="RATE",
                         help="Upload limit e.g. 10M, 512K, 0 = unlimited")
    limit_p.add_argument("--down", metavar="RATE",
                         help="Download limit e.g. 50M, 1G, 0 = unlimited")

    pause_p = share_sub.add_parser("pause", help="Pause a share")
    pause_p.add_argument("share_id")

    resume_p = share_sub.add_parser("resume", help="Resume a share")
    resume_p.add_argument("share_id")

    conflict_p = share_sub.add_parser(
        "set-conflict", help="Set conflict resolution strategy for a share")
    conflict_p.add_argument("share_id")
    conflict_p.add_argument("strategy",
                            choices=["last_write_wins", "rename_conflict", "ask"])

    conflicts_p = share_sub.add_parser(
        "conflicts", help="List pending conflicts (ask strategy)")
    conflicts_p.add_argument("share_id")

    resolve_p = share_sub.add_parser(
        "resolve", help="Resolve a pending conflict")
    resolve_p.add_argument("conflict_id", type=int)
    resolve_p.add_argument("resolution", choices=["keep-local", "keep-remote"])

    sub.add_parser("status", help="Show current daemon status")
    watch_p = sub.add_parser("watch", help="Stream live status events")
    watch_p.add_argument("--json", action="store_true", default=False,
                         help="Output machine-readable newline-delimited JSON")

    return p


def main():
    parser = build_parser()
    args   = parser.parse_args()

    dispatch = {
        "identity": cmd_identity,
        "status":   cmd_status,
        "watch":    cmd_watch,
    }

    if args.command in dispatch:
        dispatch[args.command](args)
    elif args.command == "share":
        share_dispatch = {
            "list":         cmd_share_list,
            "info":         cmd_share_info,
            "peers":        cmd_share_peers,
            "create":       cmd_share_create,
            "add":          cmd_share_add,
            "remove":       cmd_share_remove,
            "set-limit":    cmd_share_set_limit,
            "pause":        cmd_share_pause,
            "resume":       cmd_share_resume,
            "grant":        cmd_share_grant,
            "revoke":       cmd_share_revoke,
            "set-conflict": cmd_share_set_conflict,
            "conflicts":    cmd_share_conflicts,
            "resolve":      cmd_share_resolve,
        }
        share_dispatch[args.share_command](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
