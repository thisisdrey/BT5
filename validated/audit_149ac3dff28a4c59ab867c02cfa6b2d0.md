### Title
Per-connection (not aggregate) unstaked stream throttling allows unstaked clients to collectively saturate the shared `packet_sender` channel and starve staked traffic - ([File: streamer/src/nonblocking/stream_throttle.rs])

### Summary
The QUIC stream throttle (`StakedStreamLoadEMA`) caps each individual unstaked connection to `MAX_UNSTAKED_TPS` (200 streams/sec), but this cap is applied per-connection, not divided across the pool of concurrently allowed unstaked connections (`max_unstaked_connections`, default 2000). An attacker who opens many unstaked connections, each individually compliant with the per-connection limit, can therefore drive aggregate unstaked throughput far beyond the ~20% share (`EXPECTED_UNSTAKED_STREAMS_RATIO`) that the design intends to reserve for unstaked traffic, filling the shared, non-prioritized `packet_sender` crossbeam channel and causing legitimate staked packets to be dropped via `TrySendError::Full`.

### Finding Description
`StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` returns, for `ConnectionPeerType::Unstaked`, a fixed `max_unstaked_load_in_throttling_window` value derived from a global constant `MAX_UNSTAKED_TPS = 200` [1](#0-0) [2](#0-1) . This value is computed once in `StakedStreamLoadEMA::new` as `MAX_UNSTAKED_TPS * STREAM_THROTTLING_INTERVAL_MS / 1000`, independent of `max_unstaked_connections` [3](#0-2) , and is applied identically to every unstaked connection via `throttle_stream`, which only checks a per-connection `ConnectionStreamCounter` against this fixed budget [4](#0-3) .

Critically, the EMA/`load_in_recent_interval` tracker that could otherwise reflect system-wide saturation only accounts for **staked** load: `increment_load` increments the counter solely `if peer_type.is_staked()` [5](#0-4) . Unstaked traffic is invisible to the EMA and therefore cannot trigger `staked_throttling_enabled`, and there is no corresponding mechanism that reduces the per-connection unstaked budget as the number of concurrent unstaked connections grows.

With default configuration (`DEFAULT_MAX_UNSTAKED_CONNECTIONS = 2000` [6](#0-5) , `MAX_UNSTAKED_TPS = 200`), an attacker opening up to 2000 concurrent unstaked connections, each individually compliant with its 200 TPS per-connection cap, can generate up to ~400,000 streams/sec in aggregate — far beyond the ~20% share (`EXPECTED_UNSTAKED_STREAMS_RATIO = 0.20` of `DEFAULT_MAX_STREAMS_PER_MS = 500` streams/ms, i.e. ~100,000/sec) intended for unstaked traffic [7](#0-6) .

Every successfully-reassembled packet from both staked and unstaked connections is sent into the *same* bounded `packet_sender` crossbeam channel via `try_send` in `handle_chunks`, with no priority queuing: `TrySendError::Full` simply drops the packet and increments `total_handle_chunk_to_packet_send_full_err` regardless of the peer's stake [8](#0-7) . This channel (`TPU_CHANNEL_SIZE = 50_000`) is shared between the staked and unstaked QUIC connection tables feeding a single TPU endpoint [9](#0-8) [10](#0-9) . Once the channel fills from unstaked volume, newly arriving staked packets are dropped just as readily as unstaked ones, because `try_send` provides no stake-based prioritization at the point of insertion.

### Impact Explanation
This is a QoS/fairness evasion: per-source-compliant unstaked senders can collectively deny shared TPU ingestion capacity to legitimate, higher-priority (staked) senders, causing dropped/delayed transactions for stake-weighted clients despite each individual unstaked connection obeying its local rate limit. This falls under the "unfair/insufficient throttling & DoS of leader TPU ingestion" bounty category — a QoS evasion causing scoped impact of shared-channel starvation of staked/legitimate packets.

### Likelihood Explanation
The precondition is only that the attacker be an unpermissioned remote client able to open ordinary QUIC connections to the leader's public TPU port — no stake, keys, or special privileges required. The default `max_unstaked_connections` (2000) and per-IP connection limits (`DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER = 8`) still permit an attacker with a modest number of source IPs to open hundreds to thousands of unstaked connections and legitimately stream at the allowed per-connection rate, making this readily reproducible with standard QUIC client tooling and multiple source addresses.

### Recommendation
Make the unstaked throttling budget aggregate rather than fixed-per-connection: divide the total intended unstaked share (e.g. `EXPECTED_UNSTAKED_STREAMS_RATIO * max_streams_per_ms`) by the current number of active unstaked connections (similar to how staked capacity is already divided proportionally to stake in `available_load_capacity_in_throttling_duration`), and/or include unstaked load in the EMA so that aggregate unstaked pressure can trigger back-off. Additionally, consider separating or prioritizing the `packet_sender` channel path so staked packets are not competing for the same bounded slots as unstaked packets (e.g. reserve channel capacity or use a priority-aware sender).

### Proof of Concept
```rust
// streamer/src/nonblocking/stream_throttle.rs (new test)
#[test]
fn test_aggregate_unstaked_budget_not_bounded_by_connection_count() {
    let load_ema = StakedStreamLoadEMA::new(
        Arc::new(StreamerStats::default()),
        /* max_unstaked_connections = */ 2000,
        DEFAULT_MAX_STREAMS_PER_MS,
    );
    let per_connection_budget = load_ema
        .available_load_capacity_in_throttling_duration(ConnectionPeerType::Unstaked, 0);
    // Each of up to 2000 concurrent unstaked connections gets this same budget,
    // independent of how many other unstaked connections exist.
    let aggregate_possible = per_connection_budget * 2000;
    // Intended unstaked share of total budget over the throttling window:
    let intended_unstaked_share =
        (EXPECTED_UNSTAKED_STREAMS_RATIO * (DEFAULT_MAX_STREAMS_PER_MS as f64)
            * (STREAM_THROTTLING_INTERVAL_MS as f64)) as u64;
    assert!(
        aggregate_possible > intended_unstaked_share,
        "aggregate unstaked capacity ({aggregate_possible}) should not vastly exceed the \
         intended unstaked share ({intended_unstaked_share}), but the per-connection cap is \
         not divided by connection count"
    );
}
```
Integration-level PoC: spawn a QUIC server via `spawn_stake_weighted_qos_server` with `max_unstaked_connections: N` and one staked identity; open `N` unstaked client connections each streaming at the per-connection allowed EMA rate concurrently with one compliant staked sender; assert that `stats.total_handle_chunk_to_packet_send_full_err` grows and that the staked sender's packet delivery rate/latency degrades as `N` increases, despite every unstaked connection individually staying under its per-connection throttle.

### Citations

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-20)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L233-270)
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
```

**File:** streamer/src/quic.rs (L48-48)
```rust
pub const DEFAULT_MAX_UNSTAKED_CONNECTIONS: usize = 2000;
```

**File:** streamer/src/nonblocking/quic.rs (L814-832)
```rust
    let packet_batch = PacketBatch::Single(packet);

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

**File:** core/src/tpu.rs (L176-253)
```rust
        let (packet_sender, packet_receiver) = bounded(TPU_CHANNEL_SIZE);
        let (vote_packet_sender, vote_packet_receiver) = bounded(TPU_VOTE_CHANNEL_SIZE);
        let evicting_vote_sender =
            EvictingSender::new(vote_packet_sender.clone(), vote_packet_receiver.clone());
        let (forwarded_packet_sender, forwarded_packet_receiver) =
            bounded(TPU_FORWARD_CHANNEL_SIZE);
        let fetch_stage = FetchStage::new_with_sender(
            tpu_vote_sockets,
            exit.clone(),
            &packet_sender,
            &evicting_vote_sender,
            forwarded_packet_receiver,
            poh_recorder,
            None, // coalesce
        );

        let staked_nodes_updater_service = StakedNodesUpdaterService::new(
            exit.clone(),
            bank_forks.clone(),
            staked_nodes.clone(),
            shared_staked_nodes_overrides,
        );

        let Channels {
            non_vote_sender,
            non_vote_receiver,
            tpu_vote_sender,
            tpu_vote_receiver,
            gossip_vote_sender,
            gossip_vote_receiver,
        } = banking_tracer_channels;

        // Streamer for Votes:
        let quic_vote_sockets: Vec<QuicSocket> =
            tpu_vote_quic_sockets.into_iter().map(Into::into).collect();
        let (
            SpawnServerResult {
                endpoints: _,
                thread: tpu_vote_quic_t,
                key_updater: vote_streamer_key_updater,
            },
            _banlist,
        ) = spawn_simple_qos_server(
            "solQuicTVo",
            "quic_streamer_tpu_vote",
            quic_vote_sockets,
            keypair,
            vote_packet_sender,
            staked_nodes.clone(),
            vote_quic_server_config.quic_streamer_config,
            vote_quic_server_config.qos_config,
            cancel.clone(),
        )
        .unwrap();

        // We check on validator startup that XDP is not mixed with multihoming, so by construction
        // at this moment all the transactions_quic_sockets and transactions_forwards_quic_sockets
        // have the same bind IP:PORT.

        // Streamer for TPU
        let transactions_quic_sockets =
            into_quic_sockets(transactions_quic_sockets, quic_xdp_sender.clone());
        let SpawnServerResult {
            endpoints: _,
            thread: tpu_quic_t,
            key_updater,
        } = spawn_stake_weighted_qos_server(
            "solQuicTpu",
            "quic_streamer_tpu",
            transactions_quic_sockets,
            keypair,
            packet_sender,
            staked_nodes.clone(),
            tpu_quic_server_config.quic_streamer_config,
            tpu_quic_server_config.qos_config,
            cancel.clone(),
        )
        .unwrap();
```
