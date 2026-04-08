# peerdup

Private peer-to-peer file replication - self-hosted, no central server.

No file data passes through a central server. The registry only brokers peer
discovery; all transfers are direct peer-to-peer via libtorrent.

## How it works

```
┌─────────────────┐
│    Registry     │   gRPC / TLS  - peer discovery, ACL, presence
└────────┬────────┘
         │ announce / WatchSharePeers
  ┌──────┴──────┐
  │             │
┌─┴──────┐  ┌──┴─────┐
│Daemon A│◄─►Daemon B│  direct P2P via libtorrent
└──┬─────┘  └────────┘
   │    ╲       ╱        relay path (symmetric NAT fallback, optional)
   │     ╲     ╱
   │   ┌──┴─┴──┐
   │   │ Relay │
   │   └───────┘
   ▼ Unix socket
┌──┴──────────┐
│ peerdup CLI │
└─────────────┘
```

## Quickstart

### 1. Host the registry and relay (one always-on server)

You need a host with a public IP and a DNS A record pointing to it.
Docker and Docker Compose must be installed.

```bash
git clone https://github.com/theronconrey/peerdup
cd peerdup
./start.sh
```

`start.sh` prompts for your domain and email on first run, writes `.env`,
then starts the stack. Caddy obtains a Let's Encrypt certificate automatically.
Subsequent runs go straight to `docker compose up -d`.

### 2. Install the daemon (each machine that syncs)

```bash
curl -fsSL https://raw.githubusercontent.com/theronconrey/peerdup/main/install.sh | sh
```

Handles libtorrent, clones the repo, and installs the daemon. Works on
Fedora, Ubuntu/Debian, and macOS.

### 3. Start the daemon

```bash
~/.local/share/peerdup/daemon/start.sh
```

Prompts for your machine name, registry address, and optional settings on
first run, writes `config.toml`, then starts the daemon.

### 4. Share a folder

```bash
# Machine A - create a share and grant access
peerdup share create photos ~/Pictures
# → prints share_id + fingerprint

peerdup share grant photos <machine-b-peer-id>

# Machine B - join the share
peerdup share add <share_id> ~/Pictures

# Verify both peers are online
peerdup share peers photos
```

## Layout

```
peerdup/
├── registry/           # Registry server - peer discovery, ACL, presence
├── relay/              # Relay server - TCP rendezvous for symmetric NAT
├── daemon/             # Peer daemon + CLI
├── start.sh            # Server-side first-run setup + docker compose launcher
├── docker-compose.yml  # Registry + relay + Caddy
├── Caddyfile           # Caddy TLS config
└── .env.example        # Copy to .env if you prefer manual config
```

## CLI reference

Share commands accept either the share **name** or the full **share_id**.

```bash
peerdup identity
peerdup share list
peerdup share info   <name-or-id>
peerdup share peers  <name-or-id>
peerdup share create <name> <path> [--import-key <file>] [--conflict <strategy>]
peerdup share add    <share_id> <path> [--conflict <strategy>]
peerdup share grant  <name-or-id> <peer_id>
peerdup share revoke <name-or-id> <peer_id>
peerdup share remove <name-or-id>
peerdup share set-limit    <name-or-id> --up 10M --down 50M
peerdup share set-conflict <name-or-id> last_write_wins|rename_conflict|ask
peerdup share conflicts    <name-or-id>
peerdup share resolve      <conflict-id> keep-local|keep-remote
peerdup share pause  <name-or-id>
peerdup share resume <name-or-id>
peerdup status
peerdup watch
```

The socket path is auto-detected from `$XDG_RUNTIME_DIR` (user installs) or
`/run/peerdup/control.sock` (system installs). Override with `PEERDUP_SOCKET`.

## Conflict resolution

When two peers independently modify the same file, peerdup detects the
divergence via mismatched libtorrent info-hashes and applies the share's
conflict strategy:

| Strategy | Behaviour |
|----------|-----------|
| `last_write_wins` | Accept the remote version silently (default) |
| `rename_conflict` | Rename local files to `name.conflict.TIMESTAMP.PEERID.ext`, then accept the remote |
| `ask` | Pause the share and record the conflict; resolve manually |

```bash
# Set strategy at share creation
peerdup share create docs ~/Documents --conflict rename_conflict

# Change strategy on an existing share
peerdup share set-conflict docs ask

# List pending conflicts (ask strategy)
peerdup share conflicts docs

# Resolve
peerdup share resolve 3 keep-local
peerdup share resolve 3 keep-remote
```

`peerdup watch` emits `[CONFLICT]` events in real time.

## Relay

For peers behind symmetric NAT that can't connect directly, the relay is
included in the Docker stack and exposed on TCP port 55002. Enable it in
each daemon's config (or via `./start.sh` prompts):

```toml
[relay]
enabled = true
address = "your-domain:55002"
```

The daemon tries a direct connection and a relay bridge simultaneously.
libtorrent uses whichever connects first - the relay only adds latency
when the direct path fails.
