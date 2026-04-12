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
│  - Rate limit policies          │
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

## Security

**Identity** - every peer and share has an Ed25519 keypair. The public key IS
the identity; `peer_id` and `share_id` are base58-encoded public keys.

**Registration** - peers register by submitting their public key and a
signature over `peer_id_bytes || name`. The registry verifies the signature
before issuing a bearer token. Bearer tokens are 256-bit random values; only
their SHA-256 hash is stored.

**Announce** - each announce is signed over
`share_id || peer_id || canonical(addrs) || ttl`. Covering addresses prevents
a rogue actor from poisoning peer tables.

**Rate limiting** - the registry applies per-peer-per-share token bucket rate
limiting on the `Announce` RPC. Exceeding the limit returns `RESOURCE_EXHAUSTED`
with a `Retry-After` hint. Configurable via `[rate_limit]`; 0 disables it.

**Audit logging** - every authenticated action is written to a structured JSON
log: who, what share, from which IP, timestamp, and outcome. Append-only with
size-based rotation. Disabled by default; enable in `[audit]`.

**Transport** - TLS (or mTLS with `ca_file`) for the gRPC layer. In the Docker
stack, Caddy terminates TLS and forwards h2c to the registry internally.
New installations default to `tls = true`.

## Health monitoring

The `Health` RPC returns real operational data:

```
Status:    ok  -  v0.1.0  -  up 3d 4h
Peers:     12 registered  -  7 online now
Shares:    5 registered
DB:        ok
TTL sweep: ok
```

Status is `"ok"` when both the database and TTL sweep loop are healthy,
`"degraded"` if one fails, and `"error"` if both fail.

From a daemon connected to the registry:

```bash
peerdup registry health
peerdup registry status
```

From an operator machine (no daemon needed):

```bash
peerdup-registry-admin --registry host:50051 health
```

## Prometheus metrics

An optional `/metrics` HTTP endpoint (separate port, default 9090) is available
when `prometheus_client` is installed:

```bash
pip install prometheus-client
```

Enable in `config.toml`:

```toml
[metrics]
enabled = true
port    = 9090
```

Metrics exposed:

| Metric | Type | Description |
|--------|------|-------------|
| `peerdup_rpc_requests_total{rpc, status}` | Counter | Requests per RPC and outcome |
| `peerdup_rpc_latency_seconds{rpc}` | Histogram | RPC latency |
| `peerdup_announce_total{share}` | Counter | Announces per share |
| `peerdup_peers_online{share}` | Gauge | Online peers per share |
| `peerdup_ttl_sweep_duration_seconds` | Histogram | TTL sweep duration |
| `peerdup_active_watch_streams` | Gauge | Active WatchSharePeers subscriptions |

## gRPC API

See [`proto/registry.proto`](proto/registry.proto) for the full schema.

| RPC | Auth | Description |
|-----|------|-------------|
| `RegisterPeer` | Signature | Register a peer identity, returns bearer token |
| `GetPeer` | Bearer | Look up a peer by ID |
| `CreateShare` | Bearer | Create a named sync share |
| `GetShare` | Bearer | Get share metadata, member list, and rate limit policy |
| `AddPeerToShare` | Bearer + owner | Grant a peer access to a share |
| `RemovePeerFromShare` | Bearer + owner | Revoke access |
| `SetSharePolicy` | Bearer + owner | Set advisory upload/download rate limits |
| `Announce` | Bearer | Heartbeat: "I'm online at these addresses" |
| `GetSharePeers` | Bearer | Snapshot of current peer state and share policy |
| `WatchSharePeers` | Bearer | Server-streaming: live peer state and policy events |
| `Health` | None | Liveness check with operational counters |
| `GetShareActivity` | Bearer + owner | Peer count and last announce timestamp |

`WatchSharePeers` delivers five event types: `ONLINE`, `OFFLINE`, `UPDATED`,
`REMOVED`, and `POLICY_UPDATED`. Daemons consume `POLICY_UPDATED` events to
apply owner-set rate limits to their libtorrent handles in real time.

## Operator CLI

`peerdup-registry-admin` connects directly to the registry gRPC port - no
daemon required. Useful for server operators checking health or tailing the
audit log without access to a configured peerdup daemon.

```bash
# Health check
peerdup-registry-admin --registry host:50051 health

# With TLS
peerdup-registry-admin --registry host:443 --tls --ca-file ca.crt health

# Tail audit log
peerdup-registry-admin --registry host:50051 --audit-log /var/log/peerdup/audit.log audit tail

# Admin token via env var
PEERDUP_ADMIN_TOKEN=secret peerdup-registry-admin --registry host:50051 health
```

## Development

```bash
make install    # pip install -e ".[dev]"
make proto      # regenerate stubs after editing the .proto
make test       # run pytest suite
make cert       # generate dev TLS certs (NOT for production)
make clean      # remove generated files
```

## Configuration

See [`config.example.toml`](config.example.toml) for all options with comments.

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `0.0.0.0` | Bind address |
| `port` | `50051` | gRPC port |
| `database_url` | `sqlite:///registry.db` | SQLAlchemy URL |
| `log_level` | `INFO` | Log verbosity |
| `tls.enabled` | `true` | Enable TLS (set false only for loopback dev) |
| `tls.cert_file` | `server.crt` | TLS certificate |
| `tls.key_file` | `server.key` | TLS private key |
| `tls.ca_file` | - | CA cert for mTLS client authentication |
| `rate_limit.announce_per_minute` | `30` | Announce rate per peer per share; 0 = unlimited |
| `audit.enabled` | `false` | Enable structured JSON audit log |
| `audit.log_file` | `audit.log` | Audit log path |
| `audit.max_bytes` | `10485760` | Max size per audit log file (10 MB) |
| `audit.backup_count` | `5` | Rotated audit log files to keep |
| `metrics.enabled` | `false` | Enable Prometheus /metrics endpoint |
| `metrics.port` | `9090` | Port for metrics HTTP server |

In Docker, TLS is terminated by Caddy - the registry binds plain gRPC on
port 50051 internally and is not exposed directly to the internet.
