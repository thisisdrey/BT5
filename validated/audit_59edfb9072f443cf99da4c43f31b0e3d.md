### Title
Unstaked stream-throttling cap (`max_unstaked_load_in_throttling_window`) is applied per-connection, not globally, allowing many-IP unstaked flood to exceed `MAX_UNSTAKED_TPS` many-fold - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns a fixed `max_unstaked_load_in_throttling_window` for every `ConnectionPeerType::Unstaked` connection, and this quota is enforced independently per `ConnectionStreamCounter` (one per connection) via `throttle_stream`. Because the load EMA that governs staked/unstaked balancing only tracks staked traffic, unstaked capacity is never divided among concurrently active unstaked connections, so N distinct unstaked connections can each stream at the full `MAX_UNSTAKED_TPS`-derived rate simultaneously.

### Finding Description
`MAX_UNSTAKED_TPS` (200) is turned into `max_unstaked_load_in_throttling_window = MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` in `StakedStreamLoadEMA::new` [1](#0-0) . `available_load_capacity_in_throttling_duration` simply returns this constant for every unstaked connection with no dependency on how many unstaked connections currently exist [2](#0-1) .

Enforcement happens per-connection: `SwQos::max_streams_per_throttling_interval` calls this function and feeds the result into `throttle_stream`, which compares it against a per-connection `ConnectionStreamCounter.stream_count` that resets every `STREAM_THROTTLING_INTERVAL` [3](#0-2) [4](#0-3) . Each new connection gets its own `ConnectionStreamCounter` created in `cache_new_connection`/`try_add_connection` [5](#0-4) , so the cap is per-socket, not aggregated across the unstaked pool.

Crucially, `increment_load` (which feeds `current_load_ema` and drives `staked_throttling_enabled`) only accounts for staked traffic:
```
pub(crate) fn increment_load(&self, peer_type: ConnectionPeerType) {
    if peer_type.is_staked() {
        self.load_in_recent_interval.fetch_add(1, Ordering::Relaxed);
    }
    self.update_ema_if_needed();
}
``` [6](#0-5) 
Unstaked stream volume is never fed back into any shared/global counter, so there is no mechanism that reduces one unstaked connection's quota because other unstaked connections are also active.

An attacker only needs to open many QUIC connections from distinct source IPs (no stake required). The unstaked connection table bounds the *number* of concurrent unstaked connections to `max_unstaked_connections` (with 90%-capacity pruning) [7](#0-6) , but does not divide the per-connection throttling quota by that count. So aggregate accepted unstaked stream rate scales linearly with the number of concurrent unstaked connections up to `max_unstaked_connections * MAX_UNSTAKED_TPS`, rather than being capped near `MAX_UNSTAKED_TPS` as the constant's name and the `EXPECTED_UNSTAKED_STREAMS_RATIO` design intent (20% of total budget) imply. This lets unstaked traffic consume far more than its intended share of the streamer's ingestion capacity, degrading capacity available for staked/legitimate senders.

### Impact Explanation
This is a QoS/DoS-adjacent evasion: unprivileged, unstaked remote clients can multiply their effective ingress budget by the number of distinct connections/IPs they establish, well beyond the `MAX_UNSTAKED_TPS`=200 design cap, causing disproportionate consumption of TPU QUIC stream-processing/ingestion capacity and starving legitimate staked traffic — a QoS evasion / resource-starvation finding against the streamer's stated stake-weighted QoS guarantee.

### Likelihood Explanation
Fully feasible with only network access to the leader's public TPU QUIC port: no stake, keys, or validator control needed. It requires opening `K` distinct unstaked QUIC connections (bounded only by `max_unstaked_connections`/peer-connection limits and available source IPs), each independently obtaining the full per-connection unstaked quota. This is straightforward to reproduce with an integration/fuzz test.

### Recommendation
Track unstaked load in `StakedStreamLoadEMA` the same way staked load is tracked (remove the `is_staked()` gate in `increment_load`, or add a parallel unstaked EMA/counter), and derive each unstaked connection's per-interval quota from a value that shrinks as the number of active unstaked connections/aggregate unstaked load grows, so the sum across all unstaked connections stays bounded near `MAX_UNSTAKED_TPS` regardless of connection count.

### Proof of Concept
Integration/fuzz test plan (in `streamer/src/nonblocking/stream_throttle.rs` or a swqos integration test):
1. Construct `StakedStreamLoadEMA::new(stats, max_unstaked_connections=K, max_streams_per_ms)`.
2. Simulate `K` independent unstaked connections, each with its own `ConnectionStreamCounter`.
3. For each connection, repeatedly call `available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, total_stake)` and drive `throttle_stream` at the returned cap, over one `STREAM_THROTTLING_INTERVAL`.
4. Assert: `total_accepted_streams_across_all_K_connections == K * max_unstaked_load_in_throttling_window`.
5. Show this violates the invariant `total_accepted_unstaked_streams <= max_unstaked_load_in_throttling_window` (i.e., bounded near `MAX_UNSTAKED_TPS`), demonstrating unbounded scaling with `K`.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L64-68)
```rust
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-271)
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
        // The peer is sending faster than we're willing to read. Sleep for what's
        // left of this read interval so the peer backs off.
        let throttle_duration =
            STREAM_THROTTLING_INTERVAL.saturating_sub(throttle_interval_start.elapsed());

        if !throttle_duration.is_zero() {
            debug!(
                "Throttling stream from {remote_addr:?}, peer type: {peer_type:?}, \
                 max_streams_per_interval: {max_streams_per_throttling_interval}, \
                 read_interval_streams: {streams_read_in_throttle_interval} throttle_duration: \
                 {throttle_duration:?}"
            );
            stats.throttled_streams.fetch_add(1, Ordering::Relaxed);
            match peer_type {
                ConnectionPeerType::Unstaked => {
                    stats
                        .throttled_unstaked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
                ConnectionPeerType::Staked(_) => {
                    stats
                        .throttled_staked_streams
                        .fetch_add(1, Ordering::Relaxed);
                }
            }
            sleep(throttle_duration).await;
        }
    }
}
```

**File:** streamer/src/nonblocking/swqos.rs (L209-219)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L241-256)
```rust
    fn prune_unstaked_connection_table(
        &self,
        unstaked_connection_table: &mut ConnectionTable<ConnectionStreamCounter>,
        max_unstaked_connections: usize,
        stats: Arc<StreamerStats>,
    ) {
        if unstaked_connection_table.total_size >= max_unstaked_connections {
            // Prune the connection table down to 90% capacity
            const PRUNE_TABLE_RATIO: f64 = 0.90;
            let max_connections = (PRUNE_TABLE_RATIO * (max_unstaked_connections as f64)) as usize;
            let num_pruned = unstaked_connection_table.prune_oldest(max_connections);
            stats
                .num_evictions_unstaked
                .fetch_add(num_pruned, Ordering::Relaxed);
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L292-298)
```rust
    fn max_streams_per_throttling_interval(&self, conn_context: &SwQosConnectionContext) -> u64 {
        self.staked_stream_load_ema
            .available_load_capacity_in_throttling_duration(
                conn_context.peer_type,
                conn_context.total_stake,
            )
    }
```
