### Title
Per-connection unstaked stream quota is a fixed guarantee, not scaled by the number of unstaked connections, letting Sybil'd unstaked QUIC connections exceed the intended ~20% throughput reservation - (File: `streamer/src/nonblocking/stream_throttle.rs`)

### Summary
The Zap Protocol bug granted every address a flat "guaranteed" `maxAllocation`, independent of how many addresses existed, so an attacker could Sybil across addresses to multiply that guaranteed allocation far past the intended cap. Agave's stake-weighted QUIC QoS (`SwQos`) has the same structural pattern: unstaked (unauthenticated) connections each receive an identical, fixed stream-throughput entitlement that is *not* divided by the actual number of concurrent unstaked connections, even though the system's design intent is that unstaked traffic as a whole should only consume a bounded (~20%) share of total stream throughput.

### Finding Description
`StakedStreamLoadEMA::new` computes a single global unstaked budget from the constant `MAX_UNSTAKED_TPS` (200) and comments that this is the "Expected fraction of max TPS to be consumed by unstaked connections" (`EXPECTED_UNSTAKED_STREAMS_RATIO = 0.20`): [1](#0-0) [2](#0-1) 

However, `available_load_capacity_in_throttling_duration` hands out this budget **per connection**, not divided across the total number of unstaked connections currently open: [3](#0-2) 

Because `ConnectionPeerType::Unstaked` always maps to the same fixed `max_unstaked_load_in_throttling_window`, every unstaked connection is independently entitled to the *entire* nominal "unstaked share," regardless of how many other unstaked connections exist simultaneously. This mirrors the Zap `calculateMaxAllocation` flaw: a guaranteed floor is granted per identity rather than scaled to the collective cap it was meant to represent.

Unstaked connections require no stake or identity verification (`remote_pubkey: None` in `build_connection_context`): [4](#0-3) 

The only gates on how many such connections an attacker can open are: a global cap `max_unstaked_connections` (default 2000) and a per-peer/IP cap `max_connections_per_unstaked_peer` (default 8): [5](#0-4) [6](#0-5) 

Since the per-IP cap only limits connections from a single IP, an attacker with access to many source IPs (e.g., cheap/ephemeral IPv6 addresses or VPS pools) can approach the global `max_unstaked_connections` ceiling. Each of those connections independently gets the full nominal unstaked-TPS quota (`MAX_UNSTAKED_TPS`), so the aggregate throughput available to unstaked Sybil connections scales linearly with connection count, up to the 2000-connection ceiling, rather than being capped in aggregate at ~20% of `max_streams_per_ms`.

### Impact Explanation
Legitimate stake-weighted throttling (`staked_throttling_enabled`) is driven only by the EMA of *staked* load (`increment_load` only counts staked streams): [7](#0-6) 

This means the unstaked flood described above is invisible to the mechanism meant to protect staked bandwidth, and the actual thread/CPU/bandwidth cost of servicing a large volume of unstaked streams is paid regardless. An unprivileged, non-staked client can therefore consume substantially more of the TPU QUIC server's ingestion capacity than the documented/intended ~20% unstaked reservation, degrading transaction ingestion for legitimate stake-weighted (staked) senders — a QoS evasion of the stake-weighted quality-of-service model that TPU QUIC ingestion is specifically designed to enforce.

### Likelihood Explanation
Exploitation requires no stake, no special privileges, and no cluster participation — only the ability to open many QUIC connections to a validator's TPU port from a sufficiently diverse set of source IPs to bypass the per-IP connection cap, which is a purely network-level (not protocol/identity-level) constraint reachable by any unprivileged remote actor.

### Recommendation
Scale the per-connection unstaked stream quota by the current number of active unstaked connections (e.g., divide `max_unstaked_load_in_throttling_window` by `unstaked_connection_table.total_size`, similar to how staked quota is already divided proportionally to `stake / total_stake`), so the aggregate unstaked throughput budget is actually bounded near the intended `EXPECTED_UNSTAKED_STREAMS_RATIO` share regardless of how many unstaked connections are opened.

### Proof of Concept
1. From N distinct source IPs (N approaching `max_unstaked_connections`, default 2000), each opening up to `max_connections_per_unstaked_peer` (default 8) QUIC connections to the validator's TPU/TPU-forward port without any stake or known identity.
2. Each connection independently drives streams up to `available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, _)` = `MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` (20 streams per 100ms window per connection, per the existing unit test `test_max_streams_for_unstaked_connection`): [8](#0-7) 
3. Sum across many concurrent unstaked connections vastly exceeds the single-connection-scoped budget the `EXPECTED_UNSTAKED_STREAMS_RATIO` comment implies for "unstaked connections" collectively, since no code path divides this budget by the connection count, unlike the staked path which explicitly divides by `total_stake`.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-19)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L52-68)
```rust
        let allow_unstaked_streams = max_unstaked_connections > 0;
        let max_staked_load_in_ms = if allow_unstaked_streams {
            max_streams_per_ms
                - ((EXPECTED_UNSTAKED_STREAMS_RATIO * (max_streams_per_ms as f64)) as u64)
        } else {
            max_streams_per_ms
        };

        let max_staked_load_in_ema_interval = max_staked_load_in_ms * STREAM_LOAD_EMA_INTERVAL_MS;
        let max_staked_load_in_throttling_window =
            max_staked_load_in_ms * STREAM_THROTTLING_INTERVAL_MS;

        let max_unstaked_load_in_throttling_window = if allow_unstaked_streams {
            MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
        } else {
            0
        };
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L160-165)
```rust
    pub(crate) fn increment_load(&self, peer_type: ConnectionPeerType) {
        if peer_type.is_staked() {
            self.load_in_recent_interval.fetch_add(1, Ordering::Relaxed);
        }
        self.update_ema_if_needed();
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L167-188)
```rust
    pub(crate) fn available_load_capacity_in_throttling_duration(
        &self,
        peer_type: ConnectionPeerType,
        total_stake: u64,
    ) -> u64 {
        match peer_type {
            ConnectionPeerType::Unstaked => self.max_unstaked_load_in_throttling_window,
            ConnectionPeerType::Staked(stake) => {
                if self.staked_throttling_enabled.load(Ordering::Relaxed) {
                    // 1 is added to `max_unstaked_load_in_throttling_window` to guarantee that staked
                    // clients get at least 1 more number of streams than unstaked connections.
                    self.max_staked_load_in_throttling_window
                        .saturating_mul(stake)
                        .checked_div(total_stake)
                        .unwrap_or(self.max_unstaked_load_in_throttling_window + 1)
                        .max(self.max_unstaked_load_in_throttling_window + 1)
                } else {
                    self.max_staked_load_in_throttling_window
                }
            }
        }
    }
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L283-297)
```rust
    #[test]
    fn test_max_streams_for_unstaked_connection() {
        let load_ema = Arc::new(StakedStreamLoadEMA::new(
            Arc::new(StreamerStats::default()),
            DEFAULT_MAX_UNSTAKED_CONNECTIONS,
            DEFAULT_MAX_STREAMS_PER_MS,
        ));
        assert_eq!(
            load_ema.available_load_capacity_in_throttling_duration(
                ConnectionPeerType::Unstaked,
                10000,
            ),
            20
        );
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L205-208)
```rust
        let max_connections_per_peer = match conn_context.peer_type() {
            ConnectionPeerType::Unstaked => self.config.max_connections_per_unstaked_peer,
            ConnectionPeerType::Staked(_) => self.config.max_connections_per_staked_peer,
        };
```

**File:** streamer/src/nonblocking/swqos.rs (L302-313)
```rust
    fn build_connection_context(&self, connection: &Connection) -> SwQosConnectionContext {
        let remote_address = connection.remote_address();
        get_connection_stake(connection, &self.staked_nodes).map_or(
            SwQosConnectionContext {
                peer_type: ConnectionPeerType::Unstaked,
                total_stake: 0,
                remote_pubkey: None,
                in_staked_table: false,
                remote_address,
                stream_counter: None,
                last_update: Arc::new(AtomicU64::new(timing::timestamp())),
            },
```

**File:** streamer/src/quic.rs (L41-48)
```rust
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER: usize = 8;

// allow multiple connections per ID for geo-distributed forwarders
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_STAKED_PEER: usize = 16;

pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```
