# peerdup

Private peer-to-peer file replication - self-hosted, no central server.

No file data passes through a central server. The registry only brokers peer
discovery; all transfers are direct peer-to-peer via libtorrent.

## How it works

```
┌─────────────────┐
│    Registry     │   gRPC / TLS  - peer discovery, ACL, presence (optional)
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

Two sync modes:

| Mode | Registry needed | Discovery | Access control |
|------|----------------|-----------|---------------|
| `registry` | Yes | Registry + LAN | ACL enforced by registry |
| `local` | No | LAN multicast only | Anyone on LAN with the share_id |

## Quickstart

### 1. Host the registry and relay (one always-on server)

You need a Linux host with a public IP, a DNS A record pointing to it, and
[Docker CE](https://docs.docker.com/engine/install/) installed.

```bash
git clone https://github.com/theronconrey/peerdup
cd peerdup
./start.sh
```

Prompts for your domain and email, then starts the stack. Caddy obtains a
Let's Encrypt certificate automatically. Subsequent runs skip the prompts.

> **LAN-only?** If all your peers are on the same network, you can skip this
> step and use `--local` shares instead - no server required.

### 2. Install the daemon (each machine that syncs)

```bash
curl -fsSL https://raw.githubusercontent.com/theronconrey/peerdup/main/install.sh | sh
```

Detects your distro, installs libtorrent, and sets up the daemon. Works on
Fedora, Ubuntu/Debian, and macOS.

### 3. Configure and start

```bash
peerdup-setup
```

Prompts for your machine name, registry address, and optional settings, then
starts the daemon. Leave registry blank if you're using local-only shares.

### 4. Share a folder

#### With a registry (access-controlled)

```bash
# Machine A - create share and grant access
peerdup share create photos ~/Pictures
peerdup share grant photos <machine-b-peer-id>

# Machine B - join
peerdup share add <share_id> ~/Pictures
peerdup share peers photos
```

#### Local-only (LAN, no registry needed)

```bash
# Machine A
peerdup share create photos ~/Pictures --local
# → prints share_id

# Machine B (same LAN)
peerdup share add <share_id> ~/Pictures --local
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
peerdup share create <name> <path> [--local] [--import-key <file>] [--conflict <strategy>]
peerdup share add    <share_id> <path> [--local] [--conflict <strategy>]
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

`peerdup share list` shows a `MODE` column (`registry` or `local`) for each share.

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
peerdup share create docs ~/Documents --conflict rename_conflict
peerdup share set-conflict docs ask
peerdup share conflicts docs
peerdup share resolve 3 keep-local
peerdup share resolve 3 keep-remote
```

`peerdup watch` emits `[CONFLICT]` events in real time.

## Relay

For peers behind symmetric NAT that can't connect directly, the relay is
included in the Docker stack and exposed on TCP port 55002. Enable it via
`peerdup-setup` prompts or manually in `config.toml`:

```toml
[relay]
enabled = true
address = "your-domain:55002"
```

The daemon tries a direct connection and a relay bridge simultaneously.
libtorrent uses whichever connects first.
