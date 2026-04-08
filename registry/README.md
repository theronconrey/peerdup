# peerdup-registry

Private gRPC registry for peer-to-peer file replication. Brokers peer
discovery for a libtorrent-backed sync daemon — no file data passes through
the registry.

## Architecture

```
┌─────────────────────────────────┐
│         Registry (this)         │
│  - Peer announce (ShareID→IPs)  │
│  - Auth / access control        │
│  - Share metadata & peer state  │
│  - gRPC API + server-streaming  │
└─────────────────────────────────┘
           ▲           ▲
           │ announce  │ watch
    ┌──────┘           └──────┐
    │                         │
┌───────────┐           ┌───────────┐
│  Peer A   │◄─────────►│  Peer B   │
│ (NAS)     │  direct   │ (laptop)  │
│ libtorrent│  P2P      │ libtorrent│
└───────────┘           └───────────┘
```

The registry only brokers connections — it never touches file data.
All transfers are direct peer-to-peer via libtorrent.

## Quickstart

```bash
# 1. Install
git clone <repo>
cd peerdup-registry
make install

# 2. Generate gRPC stubs from proto
make proto

# 3. (Optional) Generate dev TLS cert
make cert

# 4. Copy and edit config
cp config.example.toml config.toml

# 5. Run
registry-server --config config.toml
```

## Security model

### Identity
Every peer and share has an **Ed25519 keypair**. The public key IS the
identity — `peer_id` and `share_id` are base58-encoded public keys.

### Registration
Peers register by submitting their public key and a signature over
`peer_id_bytes || name`. The registry verifies the signature before
issuing a bearer token. This proves the registrant holds the private key.

### Announce
Each announce is signed over `share_id || peer_id || canonical(addrs) || ttl`.
Covering the addresses prevents a rogue registry from poisoning peer tables.

### Transport
TLS (or mTLS with `ca_file`) for the gRPC transport layer. Enable in
`config.toml`. Use Let's Encrypt or your own CA in production.

### Bearer tokens
After registration, all subsequent calls use a bearer token sent as gRPC
`authorization` metadata. Tokens are 256-bit random values; only their
SHA-256 hash is stored.

## gRPC API

See [`proto/registry.proto`](proto/registry.proto) for the full schema.

| RPC | Description |
|-----|-------------|
| `RegisterPeer` | Register a peer identity, returns bearer token |
| `GetPeer` | Look up a peer by ID |
| `CreateShare` | Create a named sync share |
| `GetShare` | Get share metadata + member list |
| `AddPeerToShare` | Grant a peer access to a share |
| `RemovePeerFromShare` | Revoke access |
| `Announce` | Heartbeat: "I'm online at these addresses" |
| `GetSharePeers` | Snapshot of current peer state |
| `WatchSharePeers` | **Server-streaming**: live peer state events |
| `Health` | Liveness check |

## Development

```bash
make test       # run pytest suite
make proto      # regenerate stubs after editing the .proto
make cert       # generate dev TLS certs (self-signed, NOT for production)
make clean      # remove generated files
```

## Configuration

See [`config.example.toml`](config.example.toml) for all options.

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `0.0.0.0` | Bind address |
| `port` | `50051` | gRPC port |
| `database_url` | `sqlite:///registry.db` | SQLAlchemy URL |
| `log_level` | `INFO` | Log verbosity |
| `tls.enabled` | `false` | Enable TLS |
| `tls.cert_file` | `server.crt` | TLS certificate |
| `tls.key_file` | `server.key` | TLS private key |
| `tls.ca_file` | `null` | CA cert for mTLS client auth |

## Deployment

The registry is a single Python process with a SQLite database — portable,
no external dependencies. For production:

- Run behind a reverse proxy (nginx/caddy) or enable TLS directly
- Use PostgreSQL by setting `database_url = "postgresql://..."`
- Deploy as a systemd service (see `scripts/` for a template)
- The registry is stateless except for the database — straightforward to
  containerise with a volume mount for the DB

## Roadmap

- [ ] Client daemon (libtorrent integration)
- [ ] LAN multicast discovery (no registry needed on same subnet)
- [ ] CLI tool (`peerdup peer add`, `peerdup share create`, etc.)
- [ ] Relay server for peers behind symmetric NAT
