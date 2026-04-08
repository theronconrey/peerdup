# peerdup-daemon

Peer daemon for the peerdup private P2P file replication system.

Runs on each machine that participates in a sync. Uses inotify (via watchdog)
to detect local file changes, announces presence to the registry, and uses
libtorrent for direct peer-to-peer transfers — no file data ever passes through
the registry.

## Architecture

```
                    ┌─────────────────┐
                    │    Registry     │
                    │  (peerdup-registry) │
                    └────────┬────────┘
                             │ gRPC (TLS)
              announce / WatchSharePeers
                    ┌────────┴────────┐
                    │                 │
             ┌──────┴──────┐   ┌──────┴──────┐
             │  Daemon A   │   │  Daemon B   │
             │  (NAS)      │◄──►  (laptop)  │
             └─────────────┘   └─────────────┘
                  libtorrent  ←direct P2P→  libtorrent
                    ▲
                    │ Unix socket
             ┌──────┴──────┐
             │  peerdup CLI │
             └─────────────┘
```

## Quickstart

```bash
# 1. Install
git clone <repo>
cd peerdup-daemon
make install

# 2. Generate gRPC stubs
make proto

# 3. Configure
cp config.example.toml config.toml
# edit config.toml — set registry address, peer name, etc.

# 4. Run
peerdup-daemon --config config.toml

# 5. In another terminal — add a share
peerdup share add <share_id> /path/to/local/dir

# 6. Check status
peerdup share list
peerdup watch        # live event stream
```

## Component overview

| Module | Responsibility |
|--------|---------------|
| `daemon/identity.py` | Ed25519 keypair generation and signing |
| `daemon/config.py` | TOML config loading with CLI overrides |
| `daemon/state/db.py` | Local SQLite state (shares, files, known peers) |
| `daemon/registry/client.py` | gRPC registry client + WatchSharePeers reconnect loop |
| `daemon/watcher/fs.py` | inotify filesystem watcher with debouncing |
| `daemon/torrent/session.py` | libtorrent session, private swarm, peer injection |
| `daemon/sync/coordinator.py` | Orchestrates all the above |
| `daemon/control/servicer.py` | ControlService gRPC over Unix socket (for CLI) |
| `daemon/daemon.py` | Entrypoint, boots all components |
| `cli/peerdup.py` | CLI tool — thin gRPC client to control socket |

## CLI reference

```
peerdup identity                            # show peer_id
peerdup share list                          # list active shares
peerdup share add <share_id> <path>         # add share at runtime
peerdup share add <share_id> <path> --permission ro
peerdup share remove <share_id>             # remove share
peerdup share remove <share_id> --delete-files
peerdup share pause <share_id>              # pause syncing
peerdup share resume <share_id>             # resume syncing
peerdup status                              # snapshot status
peerdup watch                               # live status stream
```

## libtorrent on Fedora

```bash
# Fedora provides libtorrent-rasterbar Python bindings via dnf:
sudo dnf install python3-libtorrent

# Or install from PyPI (may need build tools):
pip install libtorrent
```

## Security model

**Identity**: Each peer has an Ed25519 keypair generated on first run.
The public key is the `peer_id`. The private key lives at `identity.key_file`
with 0600 permissions — the daemon refuses to start if it's world-readable.

**Announce signatures**: Every announce is signed over
`share_id || peer_id || canonical(addrs) || ttl` — covering addresses
prevents a rogue registry from injecting arbitrary peer addresses.

**Control socket**: The Unix domain socket is created with 0600 permissions,
accessible only to the daemon's user. No authentication needed — filesystem
permissions are the access control.

**Transport**: TLS to the registry. libtorrent uses protocol encryption
between peers. Set `out_enc_policy = forced` in libtorrent config for
mandatory encryption (currently set to `enabled` = prefer but allow fallback).

## Deployment (systemd)

```bash
# Create system user
sudo useradd -r -s /sbin/nologin peerdup

# Install
sudo mkdir -p /opt/peerdup-daemon /etc/peerdup /var/lib/peerdup
sudo python3 -m venv /opt/peerdup-daemon/.venv
sudo /opt/peerdup-daemon/.venv/bin/pip install -e .
sudo cp config.example.toml /etc/peerdup/config.toml
sudo chown -R peerdup:peerdup /var/lib/peerdup

# Install and start service
sudo cp scripts/peerdup-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now peerdup-daemon

# Use CLI as the peerdup user (or add yourself to peerdup group + chmod g+rw socket)
sudo -u peerdup peerdup share list
```

## Roadmap

- [ ] LAN multicast discovery (bypass registry on same subnet)
- [ ] Relay server support for symmetric NAT
- [ ] Per-share bandwidth throttling via CLI
- [ ] File conflict resolution strategy (last-write-wins vs. versioned)
- [ ] `peerdup share create` — create a new share in the registry from CLI
- [ ] Mobile clients (iOS/Android) via REST bridge
