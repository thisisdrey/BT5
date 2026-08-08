### Title
Unstaked attacker can flood the raw UDP TPU-vote ingestion path and cause fee-free eviction of legitimate votes from the bounded `EvictingSender` channel - (streamer/src/evicting_sender.rs)

### Summary
The TPU vote ingestion path accepts raw UDP packets on `tpu_vote_sockets` with no per-sender authentication, because plain UDP sockets carry no connection/identity concept, unlike the QUIC vote path guarded by `SimpleQos`/`SwQos` stake-weighted admission control. Garbage packets sent over UDP are placed directly into the bounded, eviction-on-full `EvictingSender<PacketBatch>` vote channel and are only discarded later, downstream in sigverify, meaning an unstaked attacker can occupy/evict channel capacity before that check runs.

### Finding Description
`FetchStage::new_multi_socket` spawns `streamer::receiver` threads directly on the raw `tpu_vote_sockets` (`Vec<UdpSocket>`), feeding an `EvictingSender<PacketBatch>` sized to `TPU_VOTE_CHANNEL_SIZE = 4_000` [1](#0-0) . The call hardcodes `is_staked_service = true` with the comment "only staked connections should be voting" [2](#0-1) , but this is a plain UDP receiver loop — UDP has no handshake/identity mechanism, so this flag cannot actually authenticate or reject packets from unstaked senders; the real stake-based admission control (`SwQos`/`SimpleQos` connection tables, min-stake-ratio checks) only exists on the QUIC endpoints, e.g. `spawn_simple_qos_server`/`spawn_stake_weighted_qos_server` used for `vote_quic`/`transactions_quic` sockets [3](#0-2)  and the QoS admission logic in `SwQos::build_connection_context` [4](#0-3) . No equivalent gate exists for the raw UDP vote socket.

Once ingested, any packet — valid or garbage — is placed on the `EvictingSender<PacketBatch>` channel wrapping a bounded `crossbeam_channel`. When full, `try_send` evicts the *oldest* queued message to admit the newest one [5](#0-4) . Signature verification (the only check that would discard garbage) happens later in `SigVerifyStage`/`sigverify_stage.rs`, after packets are already occupying channel slots [6](#0-5) . Since the eviction policy is purely FIFO-oldest-out with no fairness/stake weighting on this channel, a high-rate flood of unauthenticated garbage UDP packets — sent at zero cost (votes carry no fee, and there's no per-IP/per-stake rate limit at this ingestion point) — can dominate the 4,000-slot buffer and cause legitimate, but slightly older, vote packets to be evicted before sigverify ever inspects them.

### Impact Explanation
This degrades consensus vote propagation: legitimate validator votes can be silently dropped from the leader's ingestion pipeline before verification, at zero cost to the attacker (no stake, no fee, no valid signature required to occupy a channel slot). This matches the "grossly underpriced pre-fee work" / vote-channel-starvation category, since the attacker's cost (raw UDP send bandwidth) is asymmetrically cheap versus the consensus-liveness cost of dropped votes.

### Likelihood Explanation
Feasible for any remote, unstaked client that can reach the leader's public TPU vote UDP port — no gossip/staked/config access is required, only the ability to send arbitrary UDP datagrams, matching the permitted attacker model. Repeatable as long as the attacker can sustain a send rate exceeding legitimate vote arrival rate for the channel's 4,000-message window; UDP send rates from a single unprivileged host can plausibly exceed this.

### Recommendation
Apply the same class of admission control used on the QUIC paths (stake-weighted or per-IP rate limiting) to the raw UDP `tpu_vote_sockets` ingestion path before packets are queued into the `EvictingSender` vote channel, or deprecate/disable the raw UDP vote ingress entirely when QUIC voting is enabled (`vote_use_quic`), since QUIC already provides authenticated, stake-gated admission.

### Proof of Concept
Integration test plan (Rust, using existing test scaffolding in `core/src/fetch_stage.rs` and `streamer/src/evicting_sender.rs`):
1. Construct `FetchStage::new_with_sender` with a fake `tpu_vote_sockets` UDP socket bound to localhost, wired to an `EvictingSender::new_bounded(TPU_VOTE_CHANNEL_SIZE)`.
2. From an unauthenticated UDP client (no keys, no staked identity), flood the socket with random-byte packets shaped like `PacketBatch` payloads at a rate exceeding channel capacity per unit time.
3. Concurrently, from a second "legitimate" sender, submit correctly signed vote packets at a realistic ~20 TPS rate (`MAX_VOTES_PER_SECOND`).
4. Drain the `vote_receiver` and run packets through `SigVerifyStage`; assert the fraction of legitimate votes that fail to reach sigverify (evicted before being read) grows with attacker packet volume, with no compensating per-sender/stake-based drop of the garbage traffic.
5. Expected (buggy) result: eviction rate of legitimate votes scales with attacker's raw UDP throughput; a fix should bound this ratio via admission control independent of sigverify.

### Citations

**File:** core/src/fetch_stage.rs (L183-199)
```rust
        let tpu_vote_threads: Vec<_> = tpu_vote_sockets
            .into_iter()
            .enumerate()
            .map(|(i, socket)| {
                streamer::receiver(
                    format!("solRcvrTpuVot{i:02}"),
                    socket,
                    exit.clone(),
                    vote_sender.clone(),
                    recycler.clone(),
                    tpu_vote_stats.clone(),
                    coalesce,
                    true,
                    true, // only staked connections should be voting
                )
            })
            .collect();
```

**File:** core/src/tpu.rs (L208-229)
```rust
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
```

**File:** streamer/src/nonblocking/swqos.rs (L301-329)
```rust
impl QosController<SwQosConnectionContext> for SwQos {
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
            |(pubkey, stake, total_stake)| {
                // The heuristic is that the stake should be large enough to have 1 stream pass through within one throttle
                // interval during which we allow max (MAX_STREAMS_PER_MS * STREAM_THROTTLING_INTERVAL_MS) streams.

                let peer_type = {
                    let max_streams_per_ms = self.staked_stream_load_ema.max_streams_per_ms();
                    let min_stake_ratio =
                        1_f64 / (max_streams_per_ms * STREAM_THROTTLING_INTERVAL_MS) as f64;
                    let stake_ratio = stake as f64 / total_stake as f64;
                    if stake_ratio < min_stake_ratio {
                        // If it is a staked connection with ultra low stake ratio, treat it as unstaked.
                        ConnectionPeerType::Unstaked
                    } else {
                        ConnectionPeerType::Staked(stake)
                    }
                };
```

**File:** streamer/src/evicting_sender.rs (L41-66)
```rust
    fn try_send(&self, msg: T) -> std::result::Result<(), TrySendError<T>> {
        let Err(e) = self.sender.try_send(msg) else {
            return Ok(());
        };

        match e {
            // Prefer newer messages over older messages.
            TrySendError::Full(msg) => match self.receiver.try_recv() {
                Ok(older) => {
                    // Attempt to requeue the newer message.
                    // NB: if multiple senders are used, and another sender is faster than us to send() after we've popped `older`,
                    // our try_send() will fail with Full(msg), in which case we drop the new message.
                    self.sender.try_send(msg)?;
                    // Propagate the error _with the older message_.
                    Err(TrySendError::Full(older))
                }
                // Unlikely race condition -- it was just indicated that the channel is full.
                // Attempt to requeue the message.
                Err(TryRecvError::Empty) => self.sender.try_send(msg),
                // Unreachable in practice since we maintain a reference to both the sender and receiver.
                Err(TryRecvError::Disconnected) => unreachable!(),
            },
            // Unreachable in practice since we maintain a reference to both the sender and receiver.
            TrySendError::Disconnected(_) => unreachable!(),
        }
    }
```

**File:** core/src/sigverify_stage.rs (L148-219)
```rust
impl SigVerifyStage {
    pub fn new(
        packet_receiver: Receiver<PacketBatch>,
        vote_packet_receiver: Receiver<PacketBatch>,
        non_vote_sender: BankingPacketSender,
        tpu_vote_sender: BankingPacketSender,
        forward_stage_sender: Sender<(BankingPacketBatch, bool)>,
        num_workers: NonZeroUsize,
        forward_non_votes: bool,
        sharable_banks: SharableBanks,
        scheduler_priority_floor: Option<Arc<SchedulerPriorityFloor>>,
    ) -> (Self, GossipSigVerifyHandle) {
        let (gossip_verified_vote_sender, verified_vote_receiver) = unbounded();
        let non_vote_stats = SigVerifierStats::default();
        let tpu_vote_stats = SigVerifierStats::default();
        let exit = Arc::new(AtomicBool::new(false));
        let mut rng = rand::rng();
        let non_vote_deduper = Arc::new(Deduper::<2, [u8]>::new(&mut rng, DEDUPER_NUM_BITS));
        let tpu_vote_deduper = Arc::new(Deduper::<2, [u8]>::new(&mut rng, DEDUPER_NUM_BITS));
        let worker_pool = SigVerifyWorkerPool::new(
            num_workers,
            packet_receiver,
            vote_packet_receiver,
            SigVerifyWorkerSenders {
                gossip_verified_vote_sender,
                forward_stage_sender,
            },
            forward_non_votes,
            sharable_banks,
            SigVerifyWorkerState::new(
                non_vote_sender,
                non_vote_deduper.clone(),
                SigVerifyWorkerStats {
                    total_batches: non_vote_stats.total_batches.clone(),
                    total_packets: non_vote_stats.total_packets.clone(),
                    total_dedup: non_vote_stats.total_dedup.clone(),
                    total_dedup_time_us: non_vote_stats.total_dedup_time_us.clone(),
                    total_valid_packets: non_vote_stats.total_valid_packets.clone(),
                    total_verify_time_us: non_vote_stats.total_verify_time_us.clone(),
                    max_pre_send_len: non_vote_stats.max_pre_send_len.clone(),
                    eviction_drops: non_vote_stats.eviction_drops.clone(),
                    total_dropped_below_priority_floor: non_vote_stats
                        .total_dropped_below_priority_floor
                        .clone(),
                    total_priority_floor_time_us: non_vote_stats
                        .total_priority_floor_time_us
                        .clone(),
                },
                scheduler_priority_floor,
            ),
            SigVerifyWorkerState::new(
                tpu_vote_sender,
                tpu_vote_deduper.clone(),
                SigVerifyWorkerStats {
                    total_batches: tpu_vote_stats.total_batches.clone(),
                    total_packets: tpu_vote_stats.total_packets.clone(),
                    total_dedup: tpu_vote_stats.total_dedup.clone(),
                    total_dedup_time_us: tpu_vote_stats.total_dedup_time_us.clone(),
                    total_valid_packets: tpu_vote_stats.total_valid_packets.clone(),
                    total_verify_time_us: tpu_vote_stats.total_verify_time_us.clone(),
                    max_pre_send_len: tpu_vote_stats.max_pre_send_len.clone(),
                    eviction_drops: tpu_vote_stats.eviction_drops.clone(),
                    total_dropped_below_priority_floor: tpu_vote_stats
                        .total_dropped_below_priority_floor
                        .clone(),
                    total_priority_floor_time_us: tpu_vote_stats
                        .total_priority_floor_time_us
                        .clone(),
                },
                None, // votes are not dropped for priority-floor
            ),
        );
```
