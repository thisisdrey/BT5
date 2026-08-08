### Title
Stale stake/peer-type cached in `SwQosConnectionContext` for the lifetime of a QUIC connection enables persistent QoS advantage after stake decreases - (File: streamer/src/nonblocking/swqos.rs)

### Summary
The Sherlock report describes a class of bug where a per-user "bonus" value is computed and cached at the time an action is taken (staking), and is never re-evaluated even after the global parameters that determine that bonus change, letting a user retain a stale advantage indefinitely while the value is locked in. The analogous mechanism in Agave's stake-weighted QUIC QoS is `SwQos::build_connection_context`, which snapshots a peer's stake and `total_stake` once when a QUIC connection is opened and stores it in `SwQosConnectionContext`, which then persists unmodified for the entire lifetime of that (potentially very long-lived) connection.

### Finding Description
When a new QUIC connection arrives, `SwQos::build_connection_context` reads the current `staked_nodes` map once and derives `peer_type` (`ConnectionPeerType::Staked(stake)` or `Unstaked`) and `total_stake`, storing them in `SwQosConnectionContext`. [1](#0-0) 

These cached fields are used for the entire life of the connection to determine QoS treatment: `cache_new_connection` uses `conn_context.total_stake` and `conn_context.peer_type()` to compute `max_uni_streams` once via `compute_max_allowed_uni_streams_with_rtt`, and every subsequent stream on that connection is throttled using `max_streams_per_throttling_interval`, which again reads `conn_context.peer_type` and `conn_context.total_stake` from the same never-updated context object. [2](#0-1) [3](#0-2) [4](#0-3) 

Meanwhile, the canonical stake data source (`staked_nodes`) is refreshed independently and periodically (every 5 seconds) by `StakedNodesUpdaterService`, based on the root bank's current epoch stakes, and can change substantially between refreshes (e.g., when a validator's stake decreases across an epoch boundary, or `staked_nodes_overrides` change). [5](#0-4) 

There is no code path in `swqos.rs` that re-derives `peer_type`/`total_stake` for an already-open connection from the live `staked_nodes` map — `build_connection_context` is only invoked at connection-accept time (see `handle_connection` in `quic.rs`, which takes a single `context: C` argument for the whole connection loop). As long as a peer keeps its QUIC connection alive (QUIC connections are not required to be re-established every epoch), it continues to receive the QoS treatment computed from stake/total_stake at connection-open time, exactly mirroring the FrankenDAO bug where `stakedTimeBonus` is snapshotted at stake time and never revisited even though the global bonus parameters change.

### Impact Explanation
This is a QoS evasion / unfair-advantage issue: a validator whose stake decreases (e.g., unstaking most of its SOL right after opening a batch of QUIC connections, or being displaced by other new stake) keeps the `Staked(stake)`/`total_stake` ratio computed at connection-open time for as long as it keeps its connections alive, letting it continue to consume a disproportionately large share of `max_concurrent_uni_streams` and throttling budget relative to its now-diminished actual stake. Conversely, a peer whose stake grows after connecting is stuck with an under-provisioned QoS allocation until it reconnects. Because `total_stake` is also frozen in the context, this skew compounds as the overall staked set changes (new validators joining/leaving) — the cached `total_stake` denominator becomes stale, further distorting the fairness ratio used in `compute_max_allowed_uni_streams_with_rtt` and `available_load_capacity_in_throttling_duration`. This can be leveraged by a node to retain outsized transaction-ingestion priority on the TPU/TPU-forward QUIC ports after reducing its stake, at the expense of other, genuinely higher-staked peers whose connections are newer.

### Likelihood Explanation
Moderate. It requires no special validator/operator privilege — it is reachable by any unprivileged peer that can open a QUIC connection to the TPU/TPU-forward ports, which is standard behavior for any staked client. It only requires keeping a connection open across a stake change; QUIC connections are commonly long-lived transaction-submission pipes and there is no forced periodic re-authentication of stake within `swqos.rs`. The staked-nodes refresh cadence (5s) versus epoch-length stake changes makes the divergence window potentially very large (up to a full epoch) rather than transient.

### Recommendation
Periodically re-derive (or "poke") each open connection's `peer_type`/`total_stake` from the live `staked_nodes` map — e.g., recompute it on `on_new_stream`/`on_stream_finished` or via a periodic background task that walks the `staked_connection_table`/`unstaked_connection_table` and updates each `SwQosConnectionContext`'s stake-derived fields (and, if warranted, moves entries between the staked/unstaked tables and adjusts `max_concurrent_uni_streams`) rather than freezing them at connection-accept time.

### Proof of Concept
1. Node A holds stake sufficient to be classified `ConnectionPeerType::Staked(stake)` with a favorable `stake / total_stake` ratio and opens a QUIC connection to a validator's TPU port; `build_connection_context` caches this stake and `total_stake` into `SwQosConnectionContext`. [6](#0-5) 
2. Node A immediately deactivates most of its stake (or the overall stake set shifts such that its relative share should shrink).
3. `StakedNodesUpdaterService` updates the shared `staked_nodes` map on its 5s cycle, but Node A's already-open connection's `SwQosConnectionContext` is never rebuilt. [7](#0-6) 
4. Node A keeps sending streams on the same connection; `on_new_stream` still calls `max_streams_per_throttling_interval(context)` using the stale cached `peer_type`/`total_stake`, so Node A continues to receive the old, now-unwarranted throughput allocation indefinitely as long as the connection stays open. [4](#0-3)

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L196-202)
```rust
        // get current RTT and limit it to MAX_RTT_MS right away
        let rtt_millis = connection.rtt().as_millis().min(MAX_RTT_MS as u128) as u32;
        let max_uni_streams = VarInt::from_u32(compute_max_allowed_uni_streams_with_rtt(
            rtt_millis,
            conn_context.peer_type(),
            conn_context.total_stake,
        ));
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

**File:** streamer/src/nonblocking/swqos.rs (L301-341)
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

                SwQosConnectionContext {
                    peer_type,
                    total_stake,
                    remote_pubkey: Some(pubkey),
                    in_staked_table: false,
                    remote_address,
                    last_update: Arc::new(AtomicU64::new(timing::timestamp())),
                    stream_counter: None,
                }
            },
        )
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

**File:** core/src/staked_nodes_updater_service.rs (L16-42)
```rust
const STAKE_REFRESH_CYCLE: Duration = Duration::from_secs(5);

pub struct StakedNodesUpdaterService {
    thread_hdl: JoinHandle<()>,
}

impl StakedNodesUpdaterService {
    pub fn new(
        exit: Arc<AtomicBool>,
        bank_forks: Arc<RwLock<BankForks>>,
        staked_nodes: Arc<RwLock<StakedNodes>>,
        staked_nodes_overrides: Arc<RwLock<HashMap<Pubkey, u64>>>,
    ) -> Self {
        let thread_hdl = Builder::new()
            .name("solStakedNodeUd".to_string())
            .spawn(move || {
                while !exit.load(Ordering::Relaxed) {
                    let stakes = {
                        let root_bank = bank_forks.read().unwrap().root_bank();
                        root_bank.current_epoch_staked_nodes()
                    };
                    let overrides = staked_nodes_overrides.read().unwrap().clone();
                    *staked_nodes.write().unwrap() = StakedNodes::new(stakes, overrides);
                    std::thread::sleep(STAKE_REFRESH_CYCLE);
                }
            })
            .unwrap();
```
