### Title
Unstaked stream-rate throttle is enforced per-connection, not globally, allowing connection multiplication to bypass `max_streams_per_ms` - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
`throttle_stream` limits new-uni-stream creation using a per-connection `ConnectionStreamCounter`, and for unstaked peers the allowed rate (`max_unstaked_load_in_throttling_window`) is a fixed constant (`MAX_UNSTAKED_TPS`) that is completely decoupled from the configured `max_streams_per_ms` global budget. Because `StakedStreamLoadEMA::increment_load` only accumulates load for staked peers, an unstaked attacker who opens multiple concurrent connections (bounded only by `max_connections_per_unstaked_peer` per IP and `max_unstaked_connections` in aggregate) can multiply its total stream-creation rate linearly with connection count, with no aggregate/global counter tying unstaked stream ingestion back to `max_streams_per_ms`.

### Finding Description
The QUIC-level concurrent-stream cap set via `compute_max_allowed_uni_streams_with_rtt` / `connection.set_max_concurrent_uni_streams` in `cache_new_connection` [1](#0-0)  only bounds how many streams can be *simultaneously open* on one connection; it does not bound the *rate* at which new streams can be opened and closed.

The rate limiting is done separately, per connection, via `throttle_stream`, using each connection's own `ConnectionStreamCounter` [2](#0-1) , created independently for every new connection in `cache_new_connection` (`|| Arc::new(ConnectionStreamCounter::new())`) [3](#0-2) .

The per-connection budget passed to `throttle_stream` comes from `max_streams_per_throttling_interval`, which for `ConnectionPeerType::Unstaked` returns `max_unstaked_load_in_throttling_window` [4](#0-3) [5](#0-4) . That value is computed purely from the hardcoded constant `MAX_UNSTAKED_TPS = 200` [6](#0-5) [7](#0-6) , and is completely independent of the configured `max_streams_per_ms` (which only affects the staked budget calculation, see `max_staked_load_in_ms`) [8](#0-7) .

Critically, `StakedStreamLoadEMA::increment_load`, which feeds the shared/global `load_in_recent_interval` and `current_load_ema` used for adaptive staked throttling, only records load for staked peers:
```
pub(crate) fn increment_load(&self, peer_type: ConnectionPeerType) {
    if peer_type.is_staked() {
        self.load_in_recent_interval.fetch_add(1, Ordering::Relaxed);
    }
    self.update_ema_if_needed();
}
``` [9](#0-8) 
Unstaked stream creations, called from `on_stream_accepted` for every new stream regardless of peer type [10](#0-9) , therefore never contribute to any aggregate/global counter that could throttle the collective rate across multiple unstaked connections.

The only limits gating how many unstaked connections an attacker can hold open are `max_connections_per_unstaked_peer` (per source IP, default 8) [11](#0-10)  and `max_unstaked_connections` (aggregate across all unstaked peers, default 2000) [12](#0-11) , plus per-IP/connections-per-minute and overall connection-rate limiters that only bound *connection establishment* rate, not per-connection *stream* rate [13](#0-12) . None of these gate the aggregate stream-creation rate.

As a result, since each unstaked connection independently gets its own `MAX_UNSTAKED_TPS`-based budget, an attacker distributing connections across enough source IPs (to stay within the per-IP `max_connections_per_unstaked_peer` limit) up to `max_unstaked_connections` can achieve an aggregate unstaked stream-creation rate of up to `max_unstaked_connections * MAX_UNSTAKED_TPS` (e.g. with defaults, 2000 * 200 = 400,000 streams/sec), which is neither bounded by nor reconciled against the configured `max_streams_per_ms` global rate (default 500,000/sec, of which only ~20% i.e. 100,000/sec is nominally the "expected" unstaked share, per `EXPECTED_UNSTAKED_STREAMS_RATIO`) [14](#0-13) . That 20% ratio is used only to shrink the *staked* budget; it is never enforced as an actual cap on unstaked traffic.

### Impact Explanation
This falls under Agave's "QoS evasion" bounty category: an unstaked, unprivileged remote attacker can multiply available stream-processing throughput far beyond what the per-connection throttle intends, purely by opening many connections (which is unprivileged and requires no stake). This lets one attacker monopolize a disproportionate share of the leader's TPU stream-processing capacity relative to other unstaked senders and, since the global EMA / staked-throttling logic never "sees" this unstaked load, it also cannot be leveraged to protect staked traffic beyond the raw connection-table separation of staked vs. unstaked connections.

### Likelihood Explanation
Fully reachable by an unstaked identity with no special privileges: open connections up to `max_connections_per_unstaked_peer` per source IP, spread across enough IPs to approach `max_unstaked_connections`, and on each connection open uni streams as fast as allowed by that connection's own `ConnectionStreamCounter` budget. This is deterministic and repeatable given only network access to the TPU/TPU-forward QUIC endpoint; no timing races or privileged state are required.

### Recommendation
Introduce a shared/global token-bucket (or extend `StakedStreamLoadEMA`) that accounts for unstaked stream creation across *all* unstaked connections in aggregate (not per-connection), and derive the unstaked per-connection allowance from remaining global capacity rather than from the static `MAX_UNSTAKED_TPS` constant, so total unstaked throughput is provably bounded relative to `max_streams_per_ms`.

### Proof of Concept
Integration test plan (extending existing tests in `streamer/src/nonblocking/quic.rs`, e.g. `test_throttling_check_no_packet_drop`):
1. Configure `SwQosConfig` with `max_connections_per_unstaked_peer = N` and `max_unstaked_connections = N` (e.g. N=50) and a low `max_streams_per_ms` (e.g. 10, i.e. intended global budget of 10,000 streams/sec).
2. Spawn N separate client QUIC endpoints (simulating distinct source IPs or using loopback aliases) each establishing one connection to the test server.
3. On each of the N connections concurrently, open uni streams as fast as possible for a fixed duration (e.g. 1 second) and record total streams accepted server-side via `stats.total_new_streams`.
4. Assert that `stats.total_new_streams` over that window exceeds `max_streams_per_ms * 1000` (the intended global bound), demonstrating that per-connection throttling scales with connection count and is not reconciled against the global configured rate — i.e., aggregate unstaked throughput is `~N * MAX_UNSTAKED_TPS`, independent of the configured `max_streams_per_ms`.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L196-224)
```rust
        // get current RTT and limit it to MAX_RTT_MS right away
        let rtt_millis = connection.rtt().as_millis().min(MAX_RTT_MS as u128) as u32;
        let max_uni_streams = VarInt::from_u32(compute_max_allowed_uni_streams_with_rtt(
            rtt_millis,
            conn_context.peer_type(),
            conn_context.total_stake,
        ));
        let remote_addr = conn_context.remote_address;

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
        {
            update_open_connections_stat(&self.stats, &connection_table_l);
            drop(connection_table_l);

            connection.set_max_concurrent_uni_streams(max_uni_streams);
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L17-17)
```rust
const MAX_UNSTAKED_TPS: u64 = 200;
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L19-19)
```rust
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;
```

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

**File:** streamer/src/quic.rs (L41-41)
```rust
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER: usize = 8;
```

**File:** streamer/src/quic.rs (L48-48)
```rust
pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

**File:** streamer/src/nonblocking/quic.rs (L456-508)
```rust
#[allow(clippy::too_many_arguments)]
async fn setup_connection<Q, C>(
    connecting: Connecting,
    rate_limiter: Arc<ConnectionRateLimiter>,
    overall_connection_rate_limiter: Arc<TokenBucket>,
    client_connection_tracker: ClientConnectionTracker,
    packet_sender: Sender<PacketBatch>,
    stats: Arc<StreamerStats>,
    server_params: Arc<QuicStreamerConfig>,
    qos: Arc<Q>,
    tasks: TaskTracker,
) where
    Q: QosController<C> + Send + Sync + 'static,
    C: ConnectionContext + Send + Sync + 'static,
{
    let from = connecting.remote_address();
    let res = timeout(QUIC_CONNECTION_HANDSHAKE_TIMEOUT, connecting).await;
    stats
        .outstanding_incoming_connection_attempts
        .fetch_sub(1, Ordering::Relaxed);
    if let Ok(connecting_result) = res {
        match connecting_result {
            Ok(new_connection) => {
                debug!("Got a connection {from:?}");
                // now that we have observed the handshake we can be certain
                // that the initiator owns an IP address, we can update rate
                // limiters on the server
                if !rate_limiter.register_connection(&from.ip()) {
                    debug!("Reject connection from {from:?} -- rate limiting exceeded");
                    stats
                        .connection_rate_limited_per_ipaddr
                        .fetch_add(1, Ordering::Relaxed);
                    new_connection.close(
                        CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                        CONNECTION_CLOSE_REASON_DISALLOWED,
                    );
                    return;
                }

                if overall_connection_rate_limiter.consume_tokens(1).is_err() {
                    debug!(
                        "Reject connection from {:?} -- total rate limiting exceeded",
                        from.ip()
                    );
                    stats
                        .connection_rate_limited_across_all
                        .fetch_add(1, Ordering::Relaxed);
                    new_connection.close(
                        CONNECTION_CLOSE_CODE_DISALLOWED.into(),
                        CONNECTION_CLOSE_REASON_DISALLOWED,
                    );
                    return;
                }
```
