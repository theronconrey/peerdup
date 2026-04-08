# peerdup-relay

TCP rendezvous relay for peerdup peers behind symmetric NAT.

When two peers can't connect directly (both behind symmetric NAT, or one
blocked by a firewall), they both connect to this relay and it pipes their
traffic transparently. No file data is inspected - the relay sees only
encrypted BitTorrent protocol bytes.

## How it works

1. Both peers connect to the relay server via TCP.
2. Each peer sends a signed hello identifying itself and the peer it wants
   to reach. The signature is verified using the peer's Ed25519 public key
   (the `peer_id` IS the verify key - no registry lookup needed).
3. Once both sides are connected, the relay stitches the two TCP streams
   together and forwards bytes in both directions.
4. The daemon exposes the relay tunnel as a local loopback port. libtorrent
   connects to it the same way it would connect to any peer - it doesn't
   know or care that a relay is involved.

The daemon tries a direct connection and a relay bridge simultaneously.
libtorrent uses whichever connects first, so the relay adds latency only
when the direct path fails.

## Deployment (recommended - Docker)

The relay ships with a Dockerfile and is included in the top-level
`docker-compose.yml`. It is started automatically by:

```bash
./start.sh   # from the repo root
```

The relay binds on port 55002 (raw TCP - not proxied by Caddy). Make sure
port 55002 is open in your firewall/security group.

## Manual deployment

```bash
pip install -e .
cp config.example.toml config.toml
# edit if needed - defaults are fine for most deployments
peerdup-relay --config config.toml
```

### systemd unit

```ini
[Unit]
Description=peerdup relay server
After=network.target

[Service]
ExecStart=peerdup-relay --config /etc/peerdup/relay.toml
Restart=on-failure
DynamicUser=yes

[Install]
WantedBy=multi-user.target
```

## Enabling in the daemon

Via `./start.sh` (prompted automatically), or manually in `config.toml`:

```toml
[relay]
enabled      = true
address      = "your-domain:55002"
pair_timeout = 120
```

## Security

- **Signatures verified** - each peer proves it holds the private key for
  its claimed `peer_id` before being admitted to the waiting pool.
- **No ACL check** - the relay does not verify share membership. This is
  enforced downstream - only peers with the correct libtorrent info-hash
  can complete the BitTorrent handshake.
- **No plaintext access** - the relay pipes raw bytes. libtorrent's protocol
  encryption runs end-to-end through the tunnel.
- **Replay protection** - the signed hello covers a Unix timestamp; hellos
  older than 60 seconds are rejected.

## Configuration

See [`config.example.toml`](config.example.toml) for all options.

| Key | Default | Description |
|-----|---------|-------------|
| `relay.host` | `0.0.0.0` | Bind address |
| `relay.port` | `55002` | TCP port |
| `relay.session_timeout` | `300` | Seconds to hold an unpaired session before dropping |
| `relay.max_waiting` | `1000` | Max concurrent unpaired sessions |
| `logging.level` | `INFO` | Log verbosity |

In Docker, these are configured via environment variables (`SESSION_TIMEOUT`,
`MAX_WAITING`, `LOG_LEVEL`) set in `.env`.
