# peerdup-registry

gRPC registry for the peerdup private P2P file replication system. Brokers
peer discovery and access control - no file data ever passes through here.

## Architecture

```
┌─────────────────────────────────┐
│         Registry (this)         │
│  - Peer announce (ShareID->IPs) │
│  - Auth / access control        │
│  - Share metadata & peer state  │
│  - gRPC API + server-streaming  │
└─────────────────────────────────┘
           ^           ^
           | announce  | watch
    +------+           +------+
    |                         |
+----------+           +----------+
|  Peer A  |<--------->|  Peer B  |
| libtorrent   direct  | libtorrent
+----------+    P2P    +----------+
```

The registry only brokers connections - it never touches file data.

## Deployment (recommended - Docker)

The registry ships with a Dockerfile and is orchestrated by the top-level
`docker-compose.yml` alongside the relay and Caddy (TLS termination).

From the repo root:

```bash
./start.sh
```

That's all. See the top-level README for full details.

## Manual deployment

If you prefer to run without Docker:

```bash
pip install -e .
./start.sh
```

Prompts for port, database URL, and log level on first run, writes
`config.toml`, then starts the registry. Subsequent runs skip the prompts.

For TLS, either terminate it with a reverse proxy (Caddy, nginx) or generate
a self-signed cert for development:

```bash
make cert
```

## Security model

**Identity** - every peer and share has an Ed25519 keypair. The public key IS
the identity; `peer_id` and `share_id` are base58-encoded public keys.

**Registration** - peers register by submitting their public key and a
signature over `peer_id_bytes || name`. The registry verifies the signature
before issuing a bearer token.

**Announce** - each announce is signed over
`share_id || peer_id || canonical(addrs) || ttl`. Covering addresses prevents
a rogue registry from poisoning peer tables.

**Transport** - TLS (or mTLS with `ca_file`) for the gRPC layer. In the Docker
stack, Caddy terminates TLS and forwards h2c to the registry internally.

**Bearer tokens** - 256-bit random values issued after registration; only their
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
| `WatchSharePeers` | Server-streaming: live peer state events |
| `Health` | Liveness check |

## Development

```bash
make install    # pip install -e ".[dev]"
make proto      # regenerate stubs after editing the .proto
make test       # run pytest suite
make cert       # generate dev TLS certs (NOT for production)
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
| `tls.enabled` | `false` | Enable TLS (handled by Caddy in Docker) |
| `tls.cert_file` | `server.crt` | TLS certificate |
| `tls.key_file` | `server.key` | TLS private key |
| `tls.ca_file` | - | CA cert for mTLS client auth |

In Docker, TLS is terminated by Caddy - the registry binds plain gRPC on
port 50051 internally and is not exposed directly to the internet.
