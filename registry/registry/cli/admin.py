"""
peerdup-registry-admin - operator CLI for peerdup registry servers.

Connects directly to the registry gRPC port (no daemon required).
Admin RPCs will be added to the proto in a future phase; for now the
connection, TLS/mTLS setup, and health probing are the primary value.
"""

from __future__ import annotations

import argparse
import os
import sys


# ── Bearer token interceptor ──────────────────────────────────────────────────

class _BearerInterceptor:
    """Injects Authorization: Bearer <token> into every outgoing call."""

    def __init__(self, token: str):
        self._token = token

    # Lazily import grpc base classes to avoid hard import at module load.
    def _make_interceptor(self):
        import grpc

        token = self._token

        class _Impl(
            grpc.UnaryUnaryClientInterceptor,
            grpc.UnaryStreamClientInterceptor,
            grpc.StreamUnaryClientInterceptor,
            grpc.StreamStreamClientInterceptor,
        ):
            def _inject(self, metadata):
                return list(metadata or []) + [("authorization", f"Bearer {token}")]

            def intercept_unary_unary(self, cont, details, req):
                return cont(details._replace(metadata=self._inject(details.metadata)), req)

            def intercept_unary_stream(self, cont, details, req):
                return cont(details._replace(metadata=self._inject(details.metadata)), req)

            def intercept_stream_unary(self, cont, details, req_iter):
                return cont(details._replace(metadata=self._inject(details.metadata)), req_iter)

            def intercept_stream_stream(self, cont, details, req_iter):
                return cont(details._replace(metadata=self._inject(details.metadata)), req_iter)

        return _Impl()


# ── Channel factory ───────────────────────────────────────────────────────────

def _make_channel(args):
    """
    Build a gRPC channel from parsed args.

    TLS is used when:
      - --tls flag is given, or
      - --ca-file / --cert-file is given (implies TLS), or
      - port is 443, 50443, or 8443 (auto-detect)

    mTLS is used when both --cert-file and --key-file are given.
    """
    import grpc

    addr     = args.registry
    use_tls  = args.tls or bool(args.ca_file) or bool(args.cert_file)

    if not use_tls:
        # Auto-detect from port
        _, _, port_str = addr.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = 50051
        if port in (443, 50443, 8443):
            use_tls = True

    if use_tls:
        root_certs  = None
        cert_chain  = None
        private_key = None

        if args.ca_file:
            with open(args.ca_file, "rb") as f:
                root_certs = f.read()

        if args.cert_file:
            with open(args.cert_file, "rb") as f:
                cert_chain = f.read()

        if args.key_file:
            with open(args.key_file, "rb") as f:
                private_key = f.read()

        creds = grpc.ssl_channel_credentials(
            root_certificates = root_certs,
            private_key       = private_key,
            certificate_chain = cert_chain,
        )
        return grpc.secure_channel(addr, creds), True

    return grpc.insecure_channel(addr), False


def _make_stub(args):
    """Return a RegistryServiceStub, optionally with bearer token injection."""
    import grpc
    from registry import registry_pb2_grpc as pb_grpc  # type: ignore

    channel, _ = _make_channel(args)

    token = args.token or os.environ.get("PEERDUP_ADMIN_TOKEN", "")
    if token:
        interceptor = _BearerInterceptor(token)._make_interceptor()
        channel = grpc.intercept_channel(channel, interceptor)

    return pb_grpc.RegistryServiceStub(channel)


def _tls_label(args) -> str:
    """Human-readable TLS mode string for display."""
    _, port_str = args.registry.rsplit(":", 1) if ":" in args.registry else (args.registry, "50051")
    use_tls = args.tls or bool(args.ca_file) or bool(args.cert_file)
    if not use_tls:
        try:
            port = int(port_str)
        except ValueError:
            port = 50051
        use_tls = port in (443, 50443, 8443)

    if not use_tls:
        return "no TLS"
    if args.cert_file:
        return "mTLS"
    return "TLS"


# ── Subcommand handlers ───────────────────────────────────────────────────────

def _fmt_uptime(seconds: int) -> str:
    """Format seconds into a human-readable uptime string."""
    if seconds <= 0:
        return "unknown"
    days    = seconds // 86400
    hours   = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "<1m"


def cmd_health(args):
    import grpc
    from registry import registry_pb2 as pb  # type: ignore

    stub = _make_stub(args)
    tls  = _tls_label(args)

    try:
        resp = stub.Health(pb.HealthRequest())
    except grpc.RpcError as e:
        print(f"error: could not reach registry at {args.registry} - {e.code()}: {e.details()}",
              file=sys.stderr)
        return 1

    status  = getattr(resp, "status", "unknown")
    version = getattr(resp, "version", "")
    uptime  = _fmt_uptime(getattr(resp, "uptime_s", 0))

    peers_reg    = getattr(resp, "peers_registered", 0)
    peers_online = getattr(resp, "peers_online_now", 0)
    shares_reg   = getattr(resp, "shares_registered", 0)
    db_ok        = getattr(resp, "db_ok", True)
    sweep_ok     = getattr(resp, "ttl_sweep_ok", True)

    status_str = status
    if status == "ok":
        status_str = "\033[1;32mok\033[0m"
    elif status in ("degraded", "error"):
        status_str = f"\033[1;31m{status}\033[0m"

    print(f"Registry:  {args.registry} ({tls})")
    version_part = f"  v{version}" if version else ""
    print(f"Status:    {status_str}{version_part}  -  up {uptime}")
    print(f"Peers:     {peers_reg} registered  -  {peers_online} online now")
    print(f"Shares:    {shares_reg} registered")
    print(f"DB:        {'ok' if db_ok else 'FAIL'}")
    print(f"TTL sweep: {'ok' if sweep_ok else 'FAIL'}")

    return 0 if status == "ok" else 2


def cmd_peers_list(args):
    # Verify connectivity first.
    import grpc
    from registry import registry_pb2 as pb  # type: ignore

    stub = _make_stub(args)
    try:
        stub.Health(pb.HealthRequest())
    except grpc.RpcError as e:
        print(f"error: could not reach registry at {args.registry} - {e.code()}: {e.details()}",
              file=sys.stderr)
        return 1

    print("Note: ListPeers RPC not yet implemented in registry proto.")
    print("      Use the registry DB directly to list all peers:")
    print("      sqlite3 registry.db 'SELECT peer_id, name, created_at FROM peers;'")
    return 0


def cmd_peers_remove(args):
    print(f"Note: Bulk peer removal is not a single registry RPC.")
    print(f"      To revoke peer '{args.peer_id}' from a specific share, use:")
    print(f"      peerdup share revoke <share_id> {args.peer_id}")
    print()
    print("      To remove from all shares, run RemovePeerFromShare per share.")
    print("      A RemovePeer admin RPC will be added to the proto in a future phase.")
    return 0


def cmd_shares_list(args):
    # Verify connectivity first.
    import grpc
    from registry import registry_pb2 as pb  # type: ignore

    stub = _make_stub(args)
    try:
        stub.Health(pb.HealthRequest())
    except grpc.RpcError as e:
        print(f"error: could not reach registry at {args.registry} - {e.code()}: {e.details()}",
              file=sys.stderr)
        return 1

    print("Note: ListShares RPC not yet implemented in registry proto.")
    print("      Query the registry DB directly:")
    print("      sqlite3 registry.db 'SELECT share_id, name, created_at FROM shares;'")
    return 0


def cmd_shares_purge(args):
    print(f"Note: Share purge is not yet a single admin RPC.")
    print(f"      To remove share '{args.share_id}' from all peers, ask each member to run:")
    print(f"      peerdup share remove {args.share_id}")
    print()
    print("      A PurgeShare admin RPC will be added to the proto in a future phase.")
    return 0


def cmd_audit_tail(args):
    audit_log = getattr(args, "audit_log", None)
    if not audit_log:
        print("Specify --audit-log PATH to tail the audit log.")
        print("Example: peerdup-registry-admin audit tail --audit-log /var/log/peerdup/audit.log")
        return 0

    lines = getattr(args, "lines", 20) or 20
    try:
        with open(audit_log, "r") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        for line in tail:
            print(line, end="")
    except FileNotFoundError:
        print(f"error: audit log not found: {audit_log}", file=sys.stderr)
        return 1
    except PermissionError:
        print(f"error: permission denied reading: {audit_log}", file=sys.stderr)
        return 1

    return 0


# ── Argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peerdup-registry-admin",
        description="Operator CLI for peerdup registry servers. "
                    "Connects directly to the registry gRPC port - no daemon required.",
    )

    # Global flags
    parser.add_argument(
        "--registry",
        default=os.environ.get("PEERDUP_REGISTRY", ""),
        metavar="HOST:PORT",
        help="Registry address (also reads PEERDUP_REGISTRY env var)",
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        default=False,
        help="Force TLS (auto-detected for ports 443/50443/8443)",
    )
    parser.add_argument(
        "--ca-file",
        default=None,
        metavar="PATH",
        help="CA certificate file for TLS server verification",
    )
    parser.add_argument(
        "--cert-file",
        default=None,
        metavar="PATH",
        help="Client certificate file for mTLS",
    )
    parser.add_argument(
        "--key-file",
        default=None,
        metavar="PATH",
        help="Client private key file for mTLS",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("PEERDUP_ADMIN_TOKEN", ""),
        metavar="TOKEN",
        help="Admin bearer token (also reads PEERDUP_ADMIN_TOKEN env var)",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        metavar="PATH",
        help="Path to the audit log file (used by 'audit tail')",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # health
    subparsers.add_parser("health", help="Check registry health")

    # peers
    peers_p = subparsers.add_parser("peers", help="Peer management")
    peers_sub = peers_p.add_subparsers(dest="peers_command", metavar="SUBCOMMAND")
    peers_sub.required = True
    peers_sub.add_parser("list", help="List registered peers")
    remove_p = peers_sub.add_parser("remove", help="Remove a peer")
    remove_p.add_argument("peer_id", help="Peer ID to remove")

    # shares
    shares_p = subparsers.add_parser("shares", help="Share management")
    shares_sub = shares_p.add_subparsers(dest="shares_command", metavar="SUBCOMMAND")
    shares_sub.required = True
    shares_sub.add_parser("list", help="List registered shares")
    purge_p = shares_sub.add_parser("purge", help="Purge a share")
    purge_p.add_argument("share_id", help="Share ID to purge")

    # audit
    audit_p = subparsers.add_parser("audit", help="Audit log operations")
    audit_sub = audit_p.add_subparsers(dest="audit_command", metavar="SUBCOMMAND")
    audit_sub.required = True
    tail_p = audit_sub.add_parser("tail", help="Tail the audit log")
    tail_p.add_argument(
        "--lines", "-n",
        type=int,
        default=20,
        metavar="N",
        help="Number of lines to show (default: 20)",
    )

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = _build_parser()
    args   = parser.parse_args()

    if not args.registry:
        parser.error(
            "Registry address is required. "
            "Use --registry HOST:PORT or set PEERDUP_REGISTRY env var."
        )

    # Dispatch
    cmd = args.command
    if cmd == "health":
        rc = cmd_health(args)
    elif cmd == "peers":
        if args.peers_command == "list":
            rc = cmd_peers_list(args)
        elif args.peers_command == "remove":
            rc = cmd_peers_remove(args)
        else:
            parser.error(f"Unknown peers subcommand: {args.peers_command}")
            rc = 1
    elif cmd == "shares":
        if args.shares_command == "list":
            rc = cmd_shares_list(args)
        elif args.shares_command == "purge":
            rc = cmd_shares_purge(args)
        else:
            parser.error(f"Unknown shares subcommand: {args.shares_command}")
            rc = 1
    elif cmd == "audit":
        if args.audit_command == "tail":
            rc = cmd_audit_tail(args)
        else:
            parser.error(f"Unknown audit subcommand: {args.audit_command}")
            rc = 1
    else:
        parser.error(f"Unknown command: {cmd}")
        rc = 1

    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
