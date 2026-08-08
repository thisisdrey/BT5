Based on the code I was able to examine, here is my analysis.

### Title
Per-connection unstaked stream throttle allows aggregate unstaked ingress to scale with connection count, exceeding EXPECTED_UNSTAKED_STREAMS_RATIO / MAX_UNSTAKED_TPS budget - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns a fixed `max_unstaked_load_in_throttling_window` (derived from the constant `MAX_UNSTAKED_TPS = 200`) for every `ConnectionPeerType::Unstaked` connection individually, and `throttle_stream` enforces this cap purely per-`ConnectionStreamCounter` (i.e., per connection) rather than against any shared/global unstaked counter. [1](#0-0) [2](#0-1)  Since `SwQos::max_streams_per_throttling_interval` and `on_new_stream` simply forward this per-connection quota to `throttle_stream` without any aggregate/global tally across all unstaked connections, the total unstaked ingress capacity scales linearly with the number of concurrently admitted unstaked connections (bounded only by `max_unstaked_connections`/`max_connections_per_unstaked_peer`), not by the intended aggregate 20% (`EXPECTED_UNSTAKED_STREAMS_RATIO`) / `MAX_UNSTAKED_TPS` budget. [3](#0-2) [4](#0-3) 

### Finding Description
`StakedStreamLoadEMA::new` computes `max_unstaked_load_in_throttling_window = MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` — a fixed value derived from the global 200 TPS unstaked budget — and this same fixed value is handed out, unmodified, to *every single* unstaked connection via `available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, _)`. [5](#0-4) [6](#0-5) 

The enforcement point, `throttle_stream`, only checks the passed-in `max_streams_per_throttling_interval` against a per-connection `ConnectionStreamCounter` (`stream_counter.stream_count`), which is per-connection state stored per entry in the connection table (see `try_add_connection` / `cache_new_connection` in `swqos.rs`, which allocates a fresh `ConnectionStreamCounter::new()` for each new connection key). [7](#0-6) [8](#0-7)  There is no shared/global atomic counter that aggregates unstaked stream consumption across connections before applying `MAX_UNSTAKED_TPS`.

Consequently, if an attacker opens `N` distinct unstaked connections (bounded by `max_connections_per_unstaked_peer` per IP and the overall `max_unstaked_connections`/`max_concurrent_connections` admission limit), each connection independently gets its own ~200 TPS budget, so the aggregate accepted unstaked stream rate can approach `N * MAX_UNSTAKED_TPS`, not the single fixed `MAX_UNSTAKED_TPS` (or the `EXPECTED_UNSTAKED_STREAMS_RATIO`-derived share) that the naming and the staked-budget calculation (`max_staked_load_in_ms = max_streams_per_ms - EXPECTED_UNSTAKED_STREAMS_RATIO * max_streams_per_ms`) assumes as the *total* unstaked draw. [9](#0-8) 

The only mitigating factor is `staked_throttling_enabled`, an EMA-based flag that shrinks staked connections' budgets when *staked* load is high, but it does not clamp aggregate unstaked load at all — `available_load_capacity_in_throttling_duration` for `Unstaked` always returns the same fixed per-connection value regardless of how many other unstaked connections exist or how much load they are collectively generating. [6](#0-5) 

### Impact Explanation
This allows unstaked/unprivileged connections in aggregate to consume a disproportionately large share of streamer/banking-stage ingress capacity relative to the documented/intended `EXPECTED_UNSTAKED_STREAMS_RATIO` (20%), degrading the QoS guarantee that staked senders receive a proportionate share of TPU bandwidth. This matches a QoS-evasion / resource-starvation category rather than memory-safety or consensus-safety bug — it does not cause a panic, invalid block, or verification bypass, but it does allow crowding-out of staked traffic beyond the designed budget.

### Likelihood Explanation
Feasibility depends entirely on how large `max_unstaked_connections` (the `max_concurrent_connections` limit for `SwQos`, computed as `(max_staked_connections + max_unstaked_connections) * 5 / 4`) is configured, and whether an attacker can establish that many distinct unstaked connections (from distinct IPs, since `max_connections_per_unstaked_peer` limits per-IP connections). [10](#0-9)  I was not able to retrieve the exact default values of `DEFAULT_MAX_UNSTAKED_CONNECTIONS`, `DEFAULT_MAX_STREAMS_PER_MS`, or `max_connections_per_unstaked_peer` in `streamer/src/quic.rs` before running out of tool iterations, so I cannot confirm the exact numeric ratio of "worst case aggregate unstaked TPS" vs. the theoretical 20% target with certainty — this is a gap in my verification.

### Recommendation
Introduce a shared, connection-count-independent aggregate limiter for unstaked traffic (e.g., a global token bucket or atomic counter shared across all unstaked `ConnectionStreamCounter`s) so that the total accepted unstaked stream rate across all connections is capped at `MAX_UNSTAKED_TPS`/`EXPECTED_UNSTAKED_STREAMS_RATIO`, rather than granting that budget independently to each connection.

### Proof of Concept
Integration test plan (extending the existing `test_throttling_check_no_packet_drop` pattern in `streamer/src/nonblocking/quic.rs`): spawn a QUIC server with `SwQosConfig` and a small `max_unstaked_connections` (e.g., 5) configured; open that many separate client connections from distinct source ports/loopback addresses; have each connection push streams as fast as `throttle_stream`'s per-connection limit allows (i.e., saturate each connection's `max_streams_per_throttling_interval`); measure `stats.total_new_streams` accepted per `STREAM_THROTTLING_INTERVAL_MS` window and assert it does not exceed `MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` in aggregate. Given the current implementation, the aggregate accepted count is expected to scale roughly linearly with the number of connections (violating the assertion), because each connection's `ConnectionStreamCounter` is throttled independently against the same fixed per-connection quota.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L52-58)
```rust
        let allow_unstaked_streams = max_unstaked_connections > 0;
        let max_staked_load_in_ms = if allow_unstaked_streams {
            max_streams_per_ms
                - ((EXPECTED_UNSTAKED_STREAMS_RATIO * (max_streams_per_ms as f64)) as u64)
        } else {
            max_streams_per_ms
        };
```

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

**File:** streamer/src/nonblocking/swqos.rs (L496-516)
```rust
    #[allow(clippy::manual_async_fn)]
    fn on_new_stream(&self, context: &SwQosConnectionContext) -> impl Future<Output = ()> + Send {
        async move {
            let peer_type = context.peer_type();
            let remote_addr = context.remote_address;
            let stream_counter: &Arc<ConnectionStreamCounter> =
                context.stream_counter.as_ref().unwrap();

            let max_streams_per_throttling_interval =
                self.max_streams_per_throttling_interval(context);

            throttle_stream(
                &self.stats,
                peer_type,
                remote_addr,
                stream_counter,
                max_streams_per_throttling_interval,
            )
            .await;
        }
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L518-522)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake

        (self.config.max_staked_connections + self.config.max_unstaked_connections) * 5 / 4
    }
```
