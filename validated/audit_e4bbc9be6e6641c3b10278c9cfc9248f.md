### Title
Unstaked QUIC connections can flood the shared TPU packet channel and force legitimate staked packets to be dropped via `TrySendError::Full` - (File: streamer/src/nonblocking/quic.rs)

### Summary
`handle_chunks` sends every finished stream's packet into a single shared bounded channel (`packet_sender`/`packet_batch_sender`) via `try_send`, and this channel is fed by both staked and unstaked connections without any per-class reservation or priority. Because the QUIC stream-admission throttle (`throttle_stream`) enforces its budget **per connection** rather than as a global aggregate across all unstaked connections, an unstaked attacker who opens many connections (up to `max_unstaked_connections`) can multiply per-connection throughput and saturate the shared channel faster than sigverify drains it, causing concurrent staked `try_send` calls to return `TrySendError::Full` and drop legitimate traffic.

### Finding Description
`handle_chunks` finishes a stream and unconditionally calls `packet_sender.try_send(packet_batch)`, incrementing drop counters but taking no further corrective action on `TrySendError::Full`: [1](#0-0) 

This `packet_sender` is the exact same `Sender<PacketBatch>` used for both the staked-weighted TPU QUIC server and its unstaked peers — it is a single bounded channel of size `TPU_CHANNEL_SIZE = 50_000` created once in `Tpu::new_with_client` and passed to `spawn_stake_weighted_qos_server`: [2](#0-1) [3](#0-2) 

Admission of streams onto this channel is rate-limited by `throttle_stream`, but the throttle counter (`ConnectionStreamCounter`) and its ceiling comparison are per-connection: [4](#0-3) [5](#0-4) 

The ceiling itself, `max_unstaked_load_in_throttling_window`, is a fixed global constant derived from `MAX_UNSTAKED_TPS = 200` and is *not* divided by the number of concurrent unstaked connections allowed by `max_unstaked_connections`: [6](#0-5) [7](#0-6) 

Consequently, each of the attacker's unstaked connections can independently push up to the same throttling-window budget, so the attacker's *aggregate* stream rate scales roughly linearly with the number of unstaked connections it is permitted to hold open (bounded only by `max_unstaked_connections` / `max_connections_per_unstaked_peer`), not by the intended ~20% (`EXPECTED_UNSTAKED_STREAMS_RATIO`) global share of total streams-per-ms capacity. Once enough finished single-chunk streams (`PacketBatch::Single`) are queued concurrently from multiple unstaked connections/threads, the shared 50,000-capacity `packet_sender` channel can be driven to full, at which point `try_send` on any thread — including one servicing a staked connection — receives `TrySendError::Full` and the staked packet is silently dropped rather than reaching sigverify/banking stage. There is no channel-level fairness, priority, or per-class reservation on `packet_sender`; all connections (staked and unstaked) race for the same fixed-capacity slot via plain `try_send`.

### Impact Explanation
This is a QoS/fairness evasion: an unprivileged, unstaked client can cause legitimate staked senders' transactions to be dropped at the TPU ingress boundary despite the staked sender fully respecting its own admission quota, because the shared downstream channel has no isolation between peer classes. This falls under "connection/stream/per-IP QoS limits bypass causing starvation of legitimate senders/TPU capacity" as scoped in the prompt.

### Likelihood Explanation
Feasibility depends on how many concurrent unstaked connections the attacker can hold (`max_unstaked_connections`, `max_connections_per_unstaked_peer`, and per-IP connection rate limits in `run_server`), which was not fully confirmed to a numeric default in this session — this is a caveat on exact quantitative severity. Structurally, however, the throttle design clearly allows aggregate unstaked throughput to scale with connection count rather than being capped globally, and the drop point (`try_send`/`TrySendError::Full`) unconditionally affects any packet racing for the same channel slot regardless of peer type, so the qualitative starvation effect is reproducible under sustained multi-connection unstaked load contending with a concurrent staked sender.

### Recommendation
Enforce the unstaked throttling budget as a true global aggregate (e.g., a shared atomic/token-bucket counter across all unstaked connections, not an independent per-connection counter compared against the same fixed ceiling), and/or give staked and unstaked packets separate channels/priority lanes into sigverify so that unstaked packets cannot occupy channel capacity needed by staked senders (e.g., a smaller dedicated unstaked queue drained after staked, or reserving channel slots for staked traffic).

### Proof of Concept
Integration test sketch (extending existing `streamer/src/nonblocking/quic.rs` test harness, e.g. near `test_throttling_check_no_packet_drop`):
1. Spin up `spawn_stake_weighted_qos_server` with a small `packet_batch_sender` bounded channel (e.g. capacity 16) and `SwQosConfig` allowing several unstaked connections (`max_unstaked_connections >= 8`).
2. Concurrently open N unstaked client connections and, for each, open+finish many minimal single-chunk uni-streams as fast as allowed by `throttle_stream` (each producing `PacketBatch::Single`), without draining the receiver.
3. Concurrently, from a staked test client (registered in `StakedNodes`), attempt to send packets and record `try_send` success/failure by observing whether packets arrive on `receiver` within a timeout.
4. Assert: with the channel saturated by unstaked traffic, the staked sender's delivery rate/success ratio falls below an expected floor (e.g., < 100%), and `stats.total_handle_chunk_to_packet_send_full_err` is nonzero for staked-attributed sends — demonstrating that `TrySendError::Full` affects staked packets despite the staked sender obeying its own throttle window.

### Citations

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

**File:** core/src/tpu.rs (L88-90)
```rust
/// Size of the channel between streamer and TPU sigverify stage. The values have been selected to
/// be conservative max of obsersed on mnb during high-load events.
const TPU_CHANNEL_SIZE: usize = 50_000;
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

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-20)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;

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
