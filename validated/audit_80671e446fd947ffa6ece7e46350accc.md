### Title
Unstaked QUIC senders can saturate the shared `packet_batch_sender` channel and starve admitted staked traffic at the `try_send` stage - (File: streamer/src/nonblocking/quic.rs)

### Summary
The QUIC streamer's stream-admission QoS (`SwQos`/`StakedStreamLoadEMA`) only throttles how fast a connection may *open new streams*; it does not provide any fairness or reservation on the single, shared bounded `packet_batch_sender` channel that both staked and unstaked connections feed into. Once a stream is admitted and finishes, `handle_chunks` unconditionally calls `packet_sender.try_send(packet_batch)` on the shared `Sender<PacketBatch>`, so an unstaked flood that fills this channel causes `TrySendError::Full` for already-admitted staked packets too, silently dropping them before sigverify/banking ever sees them.

### Finding Description
`handle_connection` reads finished streams and calls `handle_chunks`, which builds a `PacketBatch::Single` and pushes it with `packet_sender.try_send(packet_batch)` [1](#0-0) . This `packet_sender` is the same `crossbeam_channel::Sender<PacketBatch>` for *all* connections handled by the QUIC endpoint — staked and unstaked alike — as seen in `run_server`/`handle_connection` signatures [2](#0-1)  and in `spawn_stake_weighted_qos_server`, which takes one `packet_sender: Sender<PacketBatch>` shared across the whole server [3](#0-2) . At the TPU level, this single bounded channel (`bounded(TPU_CHANNEL_SIZE)`) is also shared with the UDP `FetchStage` [4](#0-3) .

The only admission control that differentiates staked vs. unstaked traffic is `StakedStreamLoadEMA`, which throttles the *rate of accepting new streams per connection* via `available_load_capacity_in_throttling_duration` (`max_unstaked_load_in_throttling_window` computed from `MAX_UNSTAKED_TPS = 200` per throttling window) [5](#0-4)  and per-connection `throttle_stream` calls in `on_new_stream` [6](#0-5) . This quota is enforced **per connection** (via each connection's own `ConnectionStreamCounter`), not globally, so an attacker who opens many unstaked connections (bounded only by `max_unstaked_connections`/per-IP connection limits, not by total throughput) can aggregate many times the "per-connection" 200 TPS budget into the same downstream channel.

Crucially, none of this throttling logic inspects or reserves capacity in the shared `packet_batch_sender`. Once packets pass admission and reach `handle_chunks`, they compete for the single bounded channel via `try_send`, and on `TrySendError::Full` the packet is simply dropped with a stat increment [7](#0-6)  — this applies identically to a staked sender's packet that happens to arrive when the channel is momentarily saturated by unstaked traffic. There is no priority-aware or class-aware dequeuing/dropping (e.g., dropping the oldest unstaked packet in favor of a staked one); it's plain FIFO `try_send`.

### Impact Explanation
This is a shared-channel fairness gap: unstaked traffic, while staying within its own per-connection QoS allotment, can occupy channel capacity that legitimate staked senders' packets need to reach sigverify/banking. Under sustained load this degrades TPU ingress fairness and can reduce the effective bandwidth available to staked/stake-weighted senders, which is the intended beneficiary of the stake-weighted QoS design. This falls into the "ingress/QoS fairness bypass — unstaked traffic starves staked traffic sharing the same downstream channel" bounty category described in the prompt scope.

### Likelihood Explanation
Feasible with only unprivileged QUIC/UDP access to the leader's public TPU port. The attacker needs multiple unstaked connections (each within its own per-connection throttle budget) finishing many small, single-chunk streams concurrently, which is well within normal unstaked client capability and does not require any stake or special config. The exact magnitude of impact depends on the configured `TPU_CHANNEL_SIZE`/channel capacity relative to number of admitted unstaked connections and consumer (sigverify) drain rate — these are tunable defaults that were not fully confirmed in this session (values for `TPU_CHANNEL_SIZE`, `DEFAULT_MAX_UNSTAKED_CONNECTIONS` were referenced but their concrete numeric values could not be retrieved before the tool budget ran out), so the degree of starvation under production-realistic defaults is not fully quantified here.

### Recommendation
Introduce class-aware backpressure/fairness on the shared ingress channel: e.g., separate bounded channels (or a priority/weighted queue) for staked vs. unstaked packet batches feeding into sigverify, with the consumer side selecting from the staked channel preferentially (similar to how `tpu_vote`/`non_vote`/`gossip_vote` already use separate channels in `banking_trace.rs`). Alternatively, reserve a minimum guaranteed slot budget for staked traffic in the shared channel, or make unstaked drops eagerly evict already-queued unstaked entries (evicting-sender-style) rather than blocking/dropping arbitrarily by arrival order.

### Proof of Concept
Integration test sketch (crossbeam-based, using `solana_streamer::nonblocking::testing_utilities::setup_quic_server` style helpers already present in `streamer/src/quic.rs` tests):

```rust
#[test]
fn test_unstaked_flood_starves_staked_try_send() {
    // 1. Spin up spawn_stake_weighted_qos_server with a tiny bounded packet_sender capacity, e.g. bounded(8).
    // 2. Register one staked keypair with nonzero stake in StakedNodes.
    // 3. Spawn N unstaked QUIC client connections (N > channel capacity), each opening/finishing
    //    minimal single-byte uni streams back-to-back at their throttle ceiling (~200 TPS budget each),
    //    without ever draining the receiver.
    // 4. Concurrently, from the staked client, open a stream and finish it, then measure how many
    //    of its packets are actually delivered on the receiver vs. dropped
    //    (via stats.total_handle_chunk_to_packet_send_full_err).
    // 5. Assert: with receiver undrained and enough unstaked flood, the staked sender's packet is
    //    also dropped (TrySendError::Full) at a rate above an acceptable floor (e.g. staked success
    //    rate < 100% despite staked sender being within its own QoS budget),
    //    demonstrating cross-class starvation on the shared `packet_batch_sender`.
}
```
Expected assertion: staked-connection packet delivery success rate drops measurably (not guaranteed) purely due to unstaked-connection channel pressure, even though the staked sender never exceeds its own throttling allotment — confirming the shared-channel fairness gap in `handle_chunks`/`packet_sender.try_send`.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L583-596)
```rust
async fn handle_connection<Q, C>(
    packet_sender: Sender<PacketBatch>,
    remote_address: SocketAddr,
    connection: Connection,
    stats: Arc<StreamerStats>,
    wait_for_chunk_timeout: Duration,
    max_stream_data_bytes: u32,
    context: C,
    qos: Arc<Q>,
    cancel: CancellationToken,
) where
    Q: QosController<C> + Send + Sync + 'static,
    C: ConnectionContext + Send + Sync + 'static,
{
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

**File:** streamer/src/quic.rs (L650-674)
```rust
pub fn spawn_stake_weighted_qos_server(
    thread_name: &'static str,
    metrics_name: &'static str,
    sockets: impl IntoIterator<Item = QuicSocket>,
    keypair: &Keypair,
    packet_sender: Sender<PacketBatch>,
    staked_nodes: Arc<RwLock<StakedNodes>>,
    quic_server_params: QuicStreamerConfig,
    qos_config: SwQosConfig,
    cancel: CancellationToken,
) -> Result<SpawnServerResult, QuicServerError> {
    let stats = Arc::<StreamerStats>::default();
    let swqos = SwQos::new(qos_config, stats.clone(), staked_nodes, cancel.clone());
    spawn_runtime_and_server(
        thread_name,
        metrics_name,
        stats,
        sockets,
        keypair,
        packet_sender,
        quic_server_params,
        swqos,
        cancel,
    )
}
```

**File:** core/src/tpu.rs (L176-190)
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
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-23)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;

pub const STREAM_THROTTLING_INTERVAL_MS: u64 = 100;
pub const STREAM_THROTTLING_INTERVAL: Duration =
    Duration::from_millis(STREAM_THROTTLING_INTERVAL_MS);
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
