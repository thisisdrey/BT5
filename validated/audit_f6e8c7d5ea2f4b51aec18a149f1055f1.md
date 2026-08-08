### Title
Shared `packet_sender` channel admits per-connection-compliant unstaked floods that starve staked traffic via indiscriminate `TrySendError::Full` drops - ([File: streamer/src/nonblocking/quic.rs])

### Summary
The QUIC ingress path enforces QoS/throttling only at the *per-connection* level (`ConnectionStreamCounter`), never as a true node-wide aggregate cap on unstaked throughput, while all packets — staked and unstaked — are funneled into a single shared bounded `packet_sender` channel with `try_send`. An unstaked attacker who opens many distinct connections (from distinct source IPs to respect the per-IP cap) and drives each one at its individually-allowed rate can multiply the effective unstaked ingress far beyond the intended ~20% fair-share, saturating the shared channel and causing legitimate staked packets to be dropped by the same undifferentiated `TrySendError::Full` path.

### Finding Description
In `streamer/src/nonblocking/stream_throttle.rs`, `StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns, for `ConnectionPeerType::Unstaked`, a fixed constant `max_unstaked_load_in_throttling_window` derived from the global `MAX_UNSTAKED_TPS` constant: [1](#0-0) 

This value is passed into `throttle_stream`, which enforces it against a **per-connection** `ConnectionStreamCounter` (populated in `swqos.rs::cache_new_connection` and consumed via `on_new_stream`): [2](#0-1) [3](#0-2) 

There is no global/aggregate counter shared across all unstaked connections — only staked traffic is tracked in the shared EMA (`increment_load` only updates on `peer_type.is_staked()`): [4](#0-3) 

Consequently, each unstaked connection independently gets up to `MAX_UNSTAKED_TPS` (200 TPS), and the total unstaked connection table can hold up to `DEFAULT_MAX_UNSTAKED_CONNECTIONS` (2000) entries, gated only by a per-IP cap of `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER` (8): [5](#0-4) 

An attacker distributing connections across ~250 source IPs (2000/8) can therefore drive aggregate unstaked throughput toward ~400,000 pkts/sec — each individual connection fully compliant with its own QoS budget — dwarfing the single shared `packet_sender` channel capacity of `TPU_CHANNEL_SIZE = 50_000`: [6](#0-5) [7](#0-6) 

Both staked and unstaked packet batches are pushed into this same channel from `handle_chunks` via plain `try_send`, with no stake-aware reservation, priority, or backpressure differentiation: [8](#0-7) 

Because `crossbeam_channel::bounded` FIFO admission is agnostic to which caller (staked or unstaked task) wins the race to enqueue, once the channel is near-full, staked packets racing against a large number of concurrently-running unstaked connection tasks can just as easily hit `TrySendError::Full` and be silently dropped (only counted in undifferentiated stats `total_handle_chunk_to_packet_send_full_err`), even though the QoS layer nominally reserves capacity for staked peers.

### Impact Explanation
This is a QoS-evasion / packet-processing denial-of-service: an unstaked, unprivileged attacker can degrade delivery of legitimate staked (fee-paying, prioritized) transactions to the leader's sigverify/banking pipeline without ever exceeding any single connection's or IP's configured limits, because the enforcement point (per connection) does not match the resource actually being contended (a single global bounded channel). This falls under the "QoS evasion" bounty category referenced in scope, since the intended stake-based prioritization guarantee is broken at the channel boundary.

### Likelihood Explanation
Feasible with only network access and QUIC client capability (no stake, no keys, no leader/gossip control). The attacker needs enough distinct source IPs to instantiate up to `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER` connections per IP up to the global `DEFAULT_MAX_UNSTAKED_CONNECTIONS` table cap, and sustained sending at the per-connection allowed throttle rate — both are ordinary, repeatable, unprivileged actions requiring no protocol violations.

### Recommendation
Enforce a true global aggregate unstaked throughput/queue-admission limit (not merely per-connection), and/or partition the shared `packet_sender` channel (or reserve headroom within it) by peer stake so unstaked packets cannot occupy capacity needed for staked traffic — e.g., use separate bounded channels for staked vs. unstaked packets merged with priority on the consumer side, or track a genuine cluster-wide unstaked load counter (mirroring `StakedStreamLoadEMA` but for unstaked traffic) that gates `on_new_stream` admission once aggregate unstaked load exceeds the intended fair-share, independent of how many distinct unstaked connections exist.

### Proof of Concept
Integration test plan (extending existing test harness in `streamer/src/nonblocking/quic.rs`, e.g. `setup_quic_server`/`test_throttling_check_no_packet_drop`):
1. Spawn a `SwQos` server with `QuicStreamerConfig::default_for_tests()` and default `SwQosConfig`, using a small `packet_sender` capacity (e.g. `bounded(50_000)` scaled down proportionally for a fast test, or keep default) fed by `spawn_stake_weighted_qos_server`.
2. Register one staked client in `StakedNodes` with meaningful stake, and open a persistent connection sending a steady stream of packets throughout the test at a rate within its allowed staked quota.
3. Concurrently, spin up N unstaked QUIC client connections (bound to distinct loopback-aliased source ports/IPs if per-IP caps are enforced via IP, or configure `max_connections_per_unstaked_peer` generously for the test) up to `DEFAULT_MAX_UNSTAKED_CONNECTIONS`, each sending at its own allowed per-connection throttle rate (`MAX_UNSTAKED_TPS`) via `open_uni`/`write_all`/`finish`.
4. Drain `receiver` (the shared `packet_sender` receiver) slowly (simulating realistic sigverify consumption) and measure: (a) `stats.total_handle_chunk_to_packet_send_full_err` > 0, and (b) the fraction of staked packets actually delivered vs. sent.
5. Assert staked delivery ratio stays above a defined floor (e.g. ≥ 95%) — the PoC would show it falls well below this floor once the unstaked flood saturates the shared channel, confirming `TrySendError::Full` drops are not stake-aware.

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

**File:** streamer/src/quic.rs (L41-51)
```rust
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER: usize = 8;

// allow multiple connections per ID for geo-distributed forwarders
pub const DEFAULT_MAX_QUIC_CONNECTIONS_PER_STAKED_PEER: usize = 16;

pub const DEFAULT_MAX_STAKED_CONNECTIONS: usize = 2000;

pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;

/// Limit to 500K PPS
pub const DEFAULT_MAX_STREAMS_PER_MS: u64 = 500;
```

**File:** core/src/tpu.rs (L88-99)
```rust
/// Size of the channel between streamer and TPU sigverify stage. The values have been selected to
/// be conservative max of obsersed on mnb during high-load events.
const TPU_CHANNEL_SIZE: usize = 50_000;

/// Size of the channel between the vote streamer and the TPU sigverify stage.
/// Chosen based on nominal voting load for a cluster with ~2000 validators + some margin.
pub(crate) const TPU_VOTE_CHANNEL_SIZE: usize = 4_000;

/// Size of the channel between the TPU forwards streamer and the fetch stage.
/// Mirrors `TPU_CHANNEL_SIZE`; the streamer uses `try_send`, so an over-full
/// channel drops packets (tracked via streamer metrics) rather than blocking.
const TPU_FORWARD_CHANNEL_SIZE: usize = 50_000;
```

**File:** core/src/tpu.rs (L176-176)
```rust
        let (packet_sender, packet_receiver) = bounded(TPU_CHANNEL_SIZE);
```

**File:** streamer/src/nonblocking/quic.rs (L816-832)
```rust
    if let Err(err) = packet_sender.try_send(packet_batch) {
        stats
            .total_handle_chunk_to_packet_send_err
            .fetch_add(1, Ordering::Relaxed);
        match err {
            TrySendError::Full(_) => {
                stats
                    .total_handle_chunk_to_packet_send_full_err
                    .fetch_add(1, Ordering::Relaxed);
            }
            TrySendError::Disconnected(_) => {
                stats
                    .total_handle_chunk_to_packet_send_disconnected_err
                    .fetch_add(1, Ordering::Relaxed);
            }
        }
        trace!("packet batch send error {err:?}");
```
