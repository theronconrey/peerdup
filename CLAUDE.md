# peerdup — Claude Code Handoff

## What this project is

peerdup is a private, self-hosted peer-to-peer file replication system — a
self-hosted alternative to Resilio Sync. No file data passes through any
central server. The registry only brokers peer discovery; all transfers are
direct peer-to-peer via libtorrent.

## Monorepo layout

```
peerdup/
├── CLAUDE.md           ← you are here
├── registry/           ← peerdup-registry: the coordination server
└── daemon/             ← peerdup-daemon: the peer daemon + CLI
```

Each subdirectory is an independent Python package with its own
`pyproject.toml`, `Makefile`, and `proto/` directory.

---

## Architecture

```
┌─────────────────────────────────┐
│     peerdup-registry            │
│  gRPC over TCP (TLS)            │
│  - Peer identity + auth         │
│  - Share ACL management         │
│  - Peer announce / discovery    │
│  - WatchSharePeers streaming    │
└──────────────┬──────────────────┘
               │ announce / WatchSharePeers
        ┌──────┴──────┐
        │             │
┌───────┴──────┐  ┌───┴──────────┐
│  daemon (A)  │  │  daemon (B)  │
│  NAS         │◄─►  laptop     │
│  libtorrent  │  │  libtorrent  │
└──────┬───────┘  └──────────────┘
       │ Unix socket
┌──────┴───────┐
│  peerdup CLI │
└──────────────┘
```

**Key principle**: The registry never touches file data. It only stores:
- Peer identities (Ed25519 public keys)
- Share ACLs (who can access what, with what permission)
- Ephemeral peer announcements (IP:port, TTL-based online/offline)

---

## Technology stack

| Concern | Technology |
|---------|-----------|
| RPC (registry ↔ daemon) | gRPC + protobuf |
| RPC (CLI ↔ daemon) | gRPC over Unix socket |
| Identity / auth | Ed25519 keypairs (PyNaCl), bearer tokens |
| File transfer | libtorrent (private swarm, DHT disabled) |
| Filesystem events | watchdog (inotify on Linux) |
| Local state | SQLite via SQLAlchemy |
| Config | TOML (Python 3.11 tomllib) |
| Logging | systemd journal (python-systemd) with stderr fallback |
| Target OS | Linux (Fedora primary) |

---

## Identity model

Every peer and every share has an **Ed25519 keypair**. The public key
(base58-encoded) IS the identity — `peer_id` and `share_id` are both
base58 Ed25519 public keys.

- Peers prove identity by signing registration requests
- Announces are signed over `share_id || peer_id || canonical(addrs) || ttl`
  — covering addresses prevents address-poisoning attacks
- Bearer tokens (256-bit random, SHA-256 stored) are issued after registration
  for subsequent calls

---

## Proto files

```
registry/proto/registry.proto   — RegistryService (10 RPCs)
daemon/proto/registry.proto     — copy of above (for stub generation)
daemon/proto/control.proto      — ControlService (13 RPCs, Unix socket)
```

**After any proto change**, regenerate stubs in both packages:
```bash
cd registry && make proto
cd daemon   && make proto
```

Generated files (`*_pb2.py`, `*_pb2_grpc.py`) are gitignored — always
regenerate before running.

---

## registry/ package

### Structure
```
registry/
├── proto/registry.proto
├── registry/
│   ├── auth/crypto.py      # Ed25519 verify, token hash, canonical addrs
│   ├── db/models.py        # SQLAlchemy models: peers, shares, share_peers, announcements
│   ├── events.py           # asyncio in-process event bus (WatchSharePeers)
│   ├── ttl.py              # Background TTL sweep — marks peers offline
│   ├── servicer/registry.py # All 10 gRPC RPCs
│   └── server.py           # Entrypoint: TLS, graceful shutdown
├── tests/test_crypto.py
├── pyproject.toml
├── Makefile
└── config.example.toml
```

### Running
```bash
cd registry
make install   # pip install -e ".[dev]"
make proto     # generate registry_pb2.py + registry_pb2_grpc.py
cp config.example.toml config.toml
peerdup-registry --config config.toml
```

### Key design notes
- **TTL sweep** (`registry/ttl.py`): runs every 30s, deletes expired
  announcements, fires `EVENT_TYPE_OFFLINE` events to the event bus
- **Event bus** (`registry/events.py`): in-process asyncio pub/sub.
  Single-process only — replace with Redis pub/sub to scale horizontally
- **WatchSharePeers** is server-streaming; clients get a snapshot on connect
  then a live delta stream. See `servicer/registry.py:WatchSharePeers`
- **Auth**: `RegisterPeer` verifies Ed25519 signature, issues bearer token.
  All subsequent calls require `authorization: Bearer <token>` metadata

---

## daemon/ package

### Structure
```
daemon/
├── proto/
│   ├── registry.proto      # copy of registry proto
│   └── control.proto       # ControlService for CLI↔daemon
├── daemon/
│   ├── identity.py         # keypair gen/load, signing helpers
│   ├── config.py           # TOML config + CLI overrides
│   ├── state/db.py         # local SQLite: LocalShare, LocalFile,
│   │                       #   KnownPeer, ShareKeypair
│   ├── registry/client.py  # gRPC registry client + reconnect loop
│   ├── watcher/fs.py       # watchdog inotify + debouncer (500ms)
│   ├── torrent/session.py  # libtorrent session (DHT off, private swarm)
│   ├── lan/discovery.py    # UDP multicast LAN discovery (239.193.0.0:49152)
│   ├── sync/coordinator.py # orchestrates all of the above
│   ├── control/servicer.py # ControlService RPCs (CLI interface)
│   └── daemon.py           # entrypoint
├── cli/peerdup.py          # CLI tool (thin gRPC client to control socket)
├── tests/test_config_identity.py
├── pyproject.toml
├── Makefile
└── config.example.toml
```

### Running
```bash
cd daemon
make install
make proto
cp config.example.toml config.toml
# edit config.toml: set registry.address, identity.name
peerdup-daemon --config config.toml
```

### ControlService (Unix socket)
The daemon exposes a second gRPC service on a Unix domain socket
(`/run/peerdup/control.sock` by default, 0600 permissions).
The CLI (`peerdup`) talks only to this socket — never directly to the registry.

### Key files to understand first
1. `daemon/sync/coordinator.py` — the orchestrator. Read this to understand
   the full data flow. One instance per daemon.
2. `daemon/proto/control.proto` — the CLI contract. All CLI features are
   defined here.
3. `daemon/state/db.py` — four tables: `local_shares`, `local_files`,
   `known_peers`, `share_keypairs`

---

## CLI reference

```bash
peerdup identity                              # show peer_id + name
peerdup share list                            # list active shares
peerdup share info <share_id>                 # single-share metadata (no peer roster)
peerdup share create <name> <path>            # create new share (generates share_id)
peerdup share add <share_id> <path>           # join existing share
peerdup share peers <share_id>                # roster: who's online, permissions
peerdup share grant <share_id> <peer_id>      # grant access (owner only)
peerdup share revoke <share_id> <peer_id>     # revoke access (owner only)
peerdup share remove <share_id>               # leave share
peerdup share pause <share_id>                # pause syncing
peerdup share resume <share_id>               # resume syncing
peerdup status                                # snapshot status
peerdup watch                                 # live event stream
```

### Typical first-time flow
```bash
# Machine A (share owner)
peerdup share create photos /mnt/nas/photos
# → prints share_id

peerdup share grant <share_id> <machine_b_peer_id>

# Machine B
peerdup share add <share_id> ~/Photos/nas-sync
peerdup share peers <share_id>   # verify both online
```

---

## What's complete

- [x] Registry: all 10 RPCs, TLS, TTL sweep, streaming, tests
- [x] Daemon: identity, config, state db, registry client, inotify watcher,
      libtorrent session, coordinator, control servicer, entrypoint
- [x] CLI: all 10 commands
- [x] Share creation flow (keypair gen → registry → local activation)
- [x] Grant/revoke access
- [x] Peer roster (`share peers`) — merges registry ACL + local cache
- [x] Integration tests: registry (38 tests) + daemon (34 tests including
      end-to-end file transfer via libtorrent + ut_metadata bootstrap)
- [x] LAN multicast discovery (`daemon/daemon/lan/discovery.py`):
      UDP multicast 239.193.0.0:49152, signed packets, ACL-gated peer
      injection into libtorrent. Wired into coordinator and daemon lifecycle.
      Config via `[lan]` section. Tests in `daemon/tests/test_lan_discovery.py`.

## What's not built yet (suggested next tasks)

### Medium priority
- [ ] **Relay server**: for peers behind symmetric NAT where direct libtorrent
      connection fails. Simplest approach: a TURN-style TCP relay that both
      peers connect to, with the registry providing relay address.
- [ ] **File conflict resolution**: currently last-write-wins (libtorrent
      behaviour). Add a strategy enum to `ShareConfig`: `last_write_wins`,
      `rename_conflict`, `ask`.

### Lower priority
- [ ] **macOS/Windows watcher**: `daemon/watcher/fs.py` uses inotify via
      watchdog which works cross-platform, but the daemon hasn't been tested
      on non-Linux. The `systemd` journal handler in `daemon.py` needs a
      fallback path already present — verify it works on macOS.
- [ ] **`peerdup share create` improvements**: currently generates a share
      keypair silently. Could print the keypair fingerprint for verification,
      or support importing an existing share_id from another machine.
- [ ] **Rate limiting per share**: expose libtorrent's per-torrent rate limits
      via `peerdup share set-limit <share_id> --up 10M --down 50M`

---

## Known issues / tech debt

1. **`coordinator.py` imports `base64`** at module level but only uses it
   indirectly via `state/db.py`. Harmless but worth noting.

2. **`ControlServicer` uses `asyncio.new_event_loop()`** per RPC call to
   bridge the sync gRPC thread pool to async coordinator methods. This works
   but is not ideal. A cleaner approach: run the coordinator on a dedicated
   event loop in a background thread and use `asyncio.run_coroutine_threadsafe`
   from the servicer. Low priority — correct as-is for home-scale use.

3. **`torrent/session.py`** calls `lt.add_files` and `lt.set_piece_hashes`
   which are blocking I/O on potentially large directories. Should be
   offloaded to `asyncio.get_event_loop().run_in_executor(None, ...)` in
   the coordinator for large shares.

4. **`_save_torrent` uses deprecated `info.metadata()`** in libtorrent 2.x.
   Should use `info.info_section()` or serialize via `lt.bencode(info.info_dict())`
   depending on libtorrent version.

---

## Development workflow

```bash
# After any proto edit:
cd registry && make proto
cd daemon   && make proto

# Run tests (no libtorrent or stubs needed):
cd registry && make test
cd daemon   && make test

# Generate dev TLS certs:
cd registry && make cert

```

---

## Config file locations (production)

| File | Path |
|------|------|
| Registry config | `/etc/peerdup/registry.toml` |
| Daemon config | `/etc/peerdup/config.toml` |
| Identity key | `/var/lib/peerdup/identity.key` (0600) |
| State DB | `/var/lib/peerdup/daemon.db` |
| Control socket | `/run/peerdup/control.sock` (0600) |
| Torrent cache | `/var/lib/peerdup/torrents/` |
| systemd units | `registry/scripts/peerdup-registry.service` |
|               | `daemon/scripts/peerdup-daemon.service` |

---

## Dependencies (key ones)

```
grpcio + grpcio-tools   gRPC runtime + protoc plugin
protobuf                protobuf runtime
pynacl                  Ed25519 signing/verification
base58                  peer_id / share_id encoding
sqlalchemy              ORM for SQLite state
watchdog                inotify filesystem events (Linux)
libtorrent              P2P file transfer (install via dnf on Fedora)
tomli                   TOML config (only needed on Python < 3.11)
systemd-python          journal logging (optional, Linux only)
```

On Fedora:
```bash
sudo dnf install python3-libtorrent
pip install -e ".[dev]"   # from registry/ or daemon/
```
