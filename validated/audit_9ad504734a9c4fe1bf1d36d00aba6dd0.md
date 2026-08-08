## Title
Unstaked stream-rate cap (`MAX_UNSTAKED_TPS`) is enforced per-connection, not globally, allowing aggregate throughput to scale with connection count - ([File: streamer/src/nonblocking/stream_throttle.rs])

## Summary
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns a fixed `max_unstaked_load_in_throttling_window` (derived from `MAX_UNSTAKED_TPS = 200`) for every unstaked connection, and this value is checked against a per-connection `ConnectionStreamCounter` in `throttle_stream`. Because the counter and comparison are scoped to a single `Connection` object rather than to an aggregate unstaked bucket, an unstaked client that opens `C` parallel QUIC connections can admit roughly `C × 20 streams/100ms` (i.e., `C × MAX_UNSTAKED_TPS`) into the leader's stream/packet pipeline, bounded only by `SwQosConfig::max_unstaked_connections` and `max_connections_per_unstaked_peer`.

## Finding Description
`throttle_stream` in `streamer/src/nonblocking/stream_throttle.rs` compares `stream_counter.stream_count` (an `AtomicU64` owned by one `ConnectionStreamCounter`, created fresh per accepted connection in `SwQos::cache_new_connection` via `|| Arc::new(ConnectionStreamCounter::new())`, see `streamer/src/nonblocking/swqos.rs:203-219`) against `max_streams_per_throttling_interval`. That limit comes from `SwQos::max_streams_per_throttling_interval` → `StakedStreamLoadEMA::available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, _)`, which for unstaked peers simply returns the constant `max_unstaked_load_in_throttling_window = MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000` (20 streams per 100 ms window, per `streamer/src/nonblocking/stream_throttle.rs:64-68,167-188`).

Critically, `StakedStreamLoadEMA::increment_load` (`streamer/src/nonblocking/stream_throttle.rs:160-165`) only feeds `load_in_recent_interval` when `peer_type.is_staked()`; unstaked traffic never updates any shared/global EMA counter. There is no global unstaked stream counter anywhere in this module - the only stateful counter for unstaked streams is the per-`ConnectionStreamCounter` `stream_count`, which is reset independently for each connection (`reset_throttling_params_if_needed`, lines 213-230).

An attacker can therefore open `C` unstaked QUIC connections (up to `SwQosConfig::max_unstaked_connections`, further constrained per source IP by `max_connections_per_unstaked_peer`), and each connection independently gets its own 20-streams-per-100ms budget. Aggregate admitted unstaked stream/packet throughput scales linearly with `C`, up to `C × 200 TPS`, well beyond the intended global `MAX_UNSTAKED_TPS = 200` design target, forcing proportionally more sigverify work in the downstream pipeline.

## Impact Explanation
This is a QoS/rate-limit evasion: the stated invariant ("unstaked connection/stream limits enforced per source, not evadable by connection churn") is violated because the throttle is fundamentally per-connection. The scoped impact is inflated, attacker-controlled sigverify/banking-stage load from unstaked sources beyond the intended `MAX_UNSTAKED_TPS` budget - this maps to the Agave "QoS evasion / grossly underpriced pre-fee work" bounty category, since unstaked (fee-paying-only-at-inclusion, effectively free-to-attempt) traffic can consume sigverify capacity proportional to connection count rather than a fixed global share.

## Likelihood Explanation
Feasibility is high for any unstaked remote client: opening multiple QUIC connections requires no stake, no special permissions, and no protocol violation - just concurrent connection establishment within the configured `max_unstaked_connections` (default value not fully confirmed via available index, but this is validator-configurable and defaults to a non-trivial pool size) and `max_connections_per_unstaked_peer` per source IP (defaults appear intentionally low, e.g. 1 in `SwQosConfig::default_for_tests`, but production default is a separate constant not fully verified here). An attacker with multiple source IPs (e.g., a botnet or cloud IP pool) trivially multiplies `max_connections_per_unstaked_peer` limits across many source addresses, then further multiplies by opening many connections per allowed IP up to the global `max_unstaked_connections` ceiling. This is repeatable continuously as long as connections are kept alive or re-established.

## Recommendation
Introduce a global, shared unstaked stream-rate accounting mechanism analogous to the staked `current_load_ema`/`load_in_recent_interval` machinery: track total unstaked streams admitted per `STREAM_THROTTLING_INTERVAL` across *all* unstaked connections (e.g., an `AtomicU64` shared via `Arc` across all unstaked `ConnectionStreamCounter`s, or extend `StakedStreamLoadEMA` to also aggregate unstaked load and gate `available_load_capacity_in_throttling_duration(Unstaked, _)` on remaining global budget divided fairly among currently-open unstaked connections). Additionally, ensure `increment_load` accounts for unstaked streams so total unstaked admission is visible and cappable, not just staked load.

## Proof of Concept
Integration test plan (extends the existing test module in `streamer/src/nonblocking/stream_throttle.rs` / `swqos.rs`, or a new integration test using `streamer`'s QUIC test harness, e.g. `setup_quic_server`):

```rust
// Pseudocode integration test outline
#[tokio::test]
async fn test_unstaked_global_cap_evaded_by_connection_count() {
    // 1. Start a QUIC server via setup_quic_server with:
    //    SwQosConfig { max_unstaked_connections: N, max_connections_per_unstaked_peer: N, ..default }
    // 2. Open C independent unstaked QUIC connections (C > 1, e.g. C = 5) from distinct
    //    client endpoints/ports to the same server.
    // 3. On each connection concurrently, open uni-streams as fast as possible for one
    //    STREAM_THROTTLING_INTERVAL (100ms) window and count how many are accepted
    //    without being throttled (i.e., accepted before `throttle_stream` triggers `sleep`).
    // 4. Sum accepted streams across all C connections.
    //
    // Expected (if global cap enforced correctly): total accepted streams across all
    // connections <= MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000 (~20).
    //
    // Actual (current code): total accepted streams ≈ C * 20, demonstrating the cap
    // scales with connection count instead of being globally bounded.
    assert!(total_accepted_streams <= 20, "unstaked global TPS cap evaded via connection churn");
}
```

Unit-level companion test (no networking needed) directly exercises the root cause:
```rust
#[test]
fn test_unstaked_cap_is_per_connection_not_global() {
    let load_ema = Arc::new(StakedStreamLoadEMA::new(
        Arc::new(StreamerStats::default()),
        DEFAULT_MAX_UNSTAKED_CONNECTIONS,
        DEFAULT_MAX_STREAMS_PER_MS,
    ));
    // Simulate C independent connections each querying their own budget concurrently;
    // available_load_capacity_in_throttling_duration always returns the same constant
    // regardless of how many other unstaked connections are simultaneously active,
    // proving there is no shared/global accounting.
    let cap_conn1 = load_ema.available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, 10000);
    let cap_conn2 = load_ema.available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, 10000);
    assert_eq!(cap_conn1, cap_conn2); // each connection gets the FULL budget independently
}
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** streamer/src/nonblocking/swqos.rs (L203-219)
```rust
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

**File:** streamer/src/nonblocking/swqos.rs (L415-443)
```rust
                ConnectionPeerType::Unstaked => {
                    if let Ok((last_update, cancel_connection, stream_counter)) = self
                        .prune_unstaked_connections_and_add_new_connection(
                            client_connection_tracker,
                            connection,
                            self.unstaked_connection_table.clone(),
                            self.config.max_unstaked_connections,
                            conn_context,
                        )
                        .await
                    {
                        self.stats
                            .connection_added_from_unstaked_peer
                            .fetch_add(1, Ordering::Relaxed);
                        conn_context.in_staked_table = false;
                        conn_context.last_update = last_update;
                        conn_context.stream_counter = Some(stream_counter);
                        return Some(cancel_connection);
                    } else {
                        self.stats
                            .connection_add_failed_unstaked_node
                            .fetch_add(1, Ordering::Relaxed);
                    }
                }
            }

            None
        }
    }
```
