# peerdup

Private peer-to-peer file replication — self-hosted, no central server.

No file data passes through a central server. The registry only brokers peer
discovery — all transfers are direct peer-to-peer via libtorrent.

## Quickstart

### 1. Install dependencies (Fedora)

```bash
sudo dnf install rb_libtorrent-python3
```

### 2. Clone and install

```bash
git clone https://github.com/theronconrey/peerdup
cd peerdup

# Registry (run once, on an always-on host)
pip install -e registry/

# Daemon (run on every machine that participates in a sync)
pip install -e daemon/
```

Proto stubs are generated automatically during `pip install` — no manual
code generation step required.

### 3. Configure

```bash
# Registry
cp registry/config.example.toml registry/config.toml
# edit: set host, port, database_url

# Daemon
cp daemon/config.example.toml daemon/config.toml
# edit: set registry.address, identity.name

```

### 4. Run

```bash
# Terminal 1 — registry
peerdup-registry --config registry/config.toml

# Terminal 2 — daemon
peerdup-daemon --config daemon/config.toml
```

### 5. Share a folder

```bash
# Create a share (Machine A — owner)
peerdup share create photos ~/Pictures
# → prints share_id + fingerprint

# Grant access to another peer
peerdup share grant photos <their-peer-id>

# Join the share (Machine B)
peerdup share add <share_id> ~/Pictures

# Verify both peers are online
peerdup share peers photos
```

## Layout

```
peerdup/
├── registry/     # Registry server — peer discovery, ACL, presence
└── daemon/       # Peer daemon + CLI
```

## CLI reference

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>
peerdup share peers  <name-or-id>
peerdup share create <name> <path> [--import-key <file>]
peerdup share add    <share_id> <path>
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit <name-or-id> --up 10M --down 50M
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch
```

Share commands accept either a share name (`photos`) or a full share_id.
The socket path is auto-detected from `$XDG_RUNTIME_DIR` (user installs) or
`/run/peerdup/control.sock` (root/system installs). Override with `PEERDUP_SOCKET`.
