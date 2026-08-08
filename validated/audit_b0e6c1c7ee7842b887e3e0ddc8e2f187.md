### Title
Per-connection stream throttling for unstaked peers is not aggregated per-IP, allowing multiplexing to bypass the unstaked TPS budget - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Finding Description
`throttle_stream` enforces `max_streams_per_throttling_interval` against a per-connection `ConnectionStreamCounter` [1](#0-0) . This counter and its `last_throttling_instant` are created fresh per QUIC connection in `SwQos::cache_new_connection` (`|| Arc::new(ConnectionStreamCounter::new())`) [2](#0-1) , and the throttling limit itself, `max_unstaked_load_in_throttling_window`, is a fixed constant derived only from the global `MAX_UNSTAKED_TPS` (200 TPS) and `STREAM_THROTTLING_INTERVAL_MS`, independent of how many connections a given source IP has open [3](#0-2) [4](#0-3) .

An unstaked client is allowed up to `max_connections_per_unstaked_peer` concurrent connections from the same IP (`ConnectionTableKey` keyed by IP when no pubkey/staked identity exists), enforced only as a connections-per-peer cap in `try_add_connection`/`cache_new_connection`, not as a shared stream-rate budget [5](#0-4) . Because each connection gets its own `ConnectionStreamCounter` and is throttled independently against the same fixed per-connection cap, an attacker who opens `N = max_connections_per_unstaked_peer` connections and sends streams evenly distributed just under the per-connection cap on each will achieve an aggregate ingestion rate of approximately `N * max_unstaked_load_in_throttling_window` streams per `STREAM_THROTTLING_INTERVAL`, i.e., up to `N` times the intended per-source unstaked budget. No component in `SwQos` aggregates stream counts across connections sharing the same source IP/pubkey key — `on_stream_accepted` only increments the EMA load (for staked traffic) and the connection-local `stream_counter` [6](#0-5) .

### Impact Explanation
This allows a single unstaked IP to consume `N`x the fair-share TPU stream/packet ingestion budget intended for unstaked peers, at the expense of other unstaked clients sharing the fixed `MAX_UNSTAKED_TPS`-derived global allotment. This matches the "QoS evasion" / unfair resource capture category — the per-connection throttle is a leaky proxy for a per-source budget and can be trivially multiplied by opening more connections (bounded only by `max_connections_per_unstaked_peer`), which is a configurable but non-trivial default (>1).

### Likelihood Explanation
Fully reachable by an unprivileged remote unstaked client: it only requires opening `max_connections_per_unstaked_peer` separate QUIC connections to the TPU and sending streams on each, staying under the per-connection cap on every connection. No staked identity, gossip, or validator control is required. This is deterministic and repeatable each `STREAM_THROTTLING_INTERVAL` (100 ms).

### Recommendation
Track and throttle unstaked (and possibly staked) stream ingestion per source IP/key across all of that peer's connections rather than per individual `Connection`, e.g., by storing a shared `ConnectionStreamCounter` (or an aggregate counter keyed by `ConnectionTableKey`) in the `ConnectionTable` entry group for that IP, and dividing `max_unstaked_load_in_throttling_window` by the number of currently open connections for that peer, or by moving the throttling decision into the connection-table entry so all connections from the same key share one counter.

### Proof of Concept
Rust unit/integration test plan in `streamer/src/nonblocking/stream_throttle.rs` or a new `swqos` test:
1. Construct `SwQos` with `SwQosConfig { max_connections_per_unstaked_peer: N (>1), max_unstaked_connections: >=N, .. }`.
2. Simulate `N` distinct QUIC connections from the same source IP (differing only in ephemeral port) via `try_add_connection`, each obtaining its own `ConnectionStreamCounter`.
3. For each connection, call `on_new_stream`/`throttle_stream` repeatedly to inject `max_streams_per_throttling_interval - 1` streams within one `STREAM_THROTTLING_INTERVAL`, asserting no connection is throttled (each individually respects its per-connection cap).
4. Sum the total streams accepted across all `N` connections in that interval and assert it is `<= max_unstaked_load_in_throttling_window` (the intended aggregate per-IP budget) — this assertion will fail, showing aggregate throughput of `~N * max_unstaked_load_in_throttling_window` exceeds the single-source budget, confirming the invariant violation.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L64-68)
```rust
        let max_unstaked_load_in_throttling_window = if allow_unstaked_streams {
            MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000
        } else {
            0
        };
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-242)
```rust
pub(crate) async fn throttle_stream(
    stats: &StreamerStats,
    peer_type: ConnectionPeerType,
    remote_addr: std::net::SocketAddr,
    stream_counter: &Arc<ConnectionStreamCounter>,
    max_streams_per_throttling_interval: u64,
) {
    let throttle_interval_start = stream_counter.reset_throttling_params_if_needed();
    let streams_read_in_throttle_interval = stream_counter.stream_count.load(Ordering::Relaxed);
    if streams_read_in_throttle_interval >= max_streams_per_throttling_interval {
```

**File:** streamer/src/nonblocking/swqos.rs (L205-219)
```rust
        let max_connections_per_peer = match conn_context.peer_type() {
            ConnectionPeerType::Unstaked => self.config.max_connections_per_unstaked_peer,
            ConnectionPeerType::Staked(_) => self.config.max_connections_per_staked_peer,
        };
        if let Some((last_update, cancel_connection, stream_counter)) = connection_table_l
            .try_add_connection(
                ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey),
                remote_addr.port(),
                client_connection_tracker,
                Some(connection.clone()),
                conn_context.peer_type(),
                conn_context.last_update.clone(),
                max_connections_per_peer,
                || Arc::new(ConnectionStreamCounter::new()),
            )
```

**File:** streamer/src/nonblocking/swqos.rs (L445-454)
```rust
    fn on_stream_accepted(&self, conn_context: &SwQosConnectionContext) {
        self.staked_stream_load_ema
            .increment_load(conn_context.peer_type);
        conn_context
            .stream_counter
            .as_ref()
            .unwrap()
            .stream_count
            .fetch_add(1, Ordering::Relaxed);
    }
```
