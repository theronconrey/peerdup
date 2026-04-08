# peerdup

Private peer-to-peer file replication. Self-hosted torrenty goodness.

No file data passes through a central server. The registry only brokers peer
discovery — all transfers are direct peer-to-peer via libtorrent.

## Quickstart

```bash
# 1. Start the registry (once, on an always-on host)
cd registry && make install && make proto
cp config.example.toml config.toml
peerdup-registry --config config.toml

# 2. Start the daemon on each machine
cd daemon && make install && make proto
cp config.example.toml config.toml
peerdup-daemon --config config.toml

# 3. Share a folder
peerdup share create photos /path/to/photos
# → prints share_id

# 4. Grant access to another peer (get their peer_id with: peerdup identity)
peerdup share grant <share_id> <their-peer-id>

# 5. On the other machine
peerdup share add <share_id> /path/to/local/dir

# 6. Check who's online
peerdup share peers <share_id>
```

## Layout

```
peerdup/
├── registry/     # Registry server (peer discovery, ACL)
└── daemon/       # Peer daemon + CLI
```
