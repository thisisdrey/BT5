Confirmed: `peer_type`/`total_stake` are only ever set once in `build_connection_context` and never mutated afterward except being copied into the context that lives for the connection's lifetime — there's no reclassification path for existing connections when `staked_nodes` is refreshed.

### Title
QUIC connections retain stale stake-based QoS classification for their entire lifetime, evading stake-weighted throttling after stake changes - (File: `streamer/src/nonblocking/swqos.rs`)

### Summary
The stake-weighted QoS controller (`SwQos`) and the simple QoS controller (`SimpleQos`) each compute a peer's `ConnectionPeerType`/`total_stake` exactly once, at connection-establishment time, via `build_connection_context()`. This snapshot is stored in the per-connection context (`SwQosConnectionContext`/`SimpleQosConnectionContext`) and reused for the entire lifetime of the QUIC connection to size the stream-rate token bucket, set `max_concurrent_uni_streams`, and compute throttling budgets — even though the authoritative stake table (`staked_nodes`) is refreshed independently every 5 seconds by `StakedNodesUpdaterService` as stake changes across epochs.

### Finding Description
`SwQos::build_connection_context` looks up the peer's stake via `get_connection_stake(connection, &self.staked_nodes)` and stores the resulting `peer_type`/`total_stake` in `SwQosConnectionContext`: [1](#0-0) 

This context is created once per connection in `setup_connection` and is cloned into the long-lived `handle_connection` task, never refreshed: [2](#0-1) 

The cached values are used to size the QUIC transport's concurrent-stream limit at connection admission time: [3](#0-2) 

and to compute the per-stream throttling budget for the entire life of the connection, on every new stream: [4](#0-3) [5](#0-4) 

Meanwhile, the source of truth (`staked_nodes`) is only refreshed on a periodic timer independent of any specific connection: [6](#0-5) 

There is no code path that iterates existing `ConnectionEntry`/`SwQosConnectionContext` instances in the `ConnectionTable` to re-derive `peer_type`/`total_stake` when `staked_nodes` changes; the only place `peer_type` is computed is `build_connection_context`, called solely from `setup_connection` at connection admission. This is directly analogous to the Munchables `LandManager::stakeMunchable()` bug: a value derived from a mutable stake source is snapshotted once at "join" time and used stale thereafter until a fresh "join" event occurs, allowing the classification to diverge from ground truth for the life of the association (here, an open QUIC connection instead of a staked plot).

### Impact Explanation
A peer that is staked (or highly staked relative to total stake) at connection-establishment time will retain the associated stake-weighted advantages — larger `max_concurrent_uni_streams` (`compute_max_allowed_uni_streams_with_rtt`) and a larger per-interval stream throughput budget from `StakedStreamLoadEMA::available_load_capacity_in_throttling_duration` — for as long as the connection remains open, even after that stake is fully deactivated/withdrawn or the peer drops out of the current epoch's stake set. Because QUIC connections can be kept alive indefinitely (idle timeout/keep-alive), and there is no mechanism to demote or close a connection whose backing stake has since gone to zero, this allows sustained evasion of the QoS/anti-DoS throttling that is supposed to bound how many transactions an unstaked/low-stake peer can push into the TPU per unit time — effectively grossly underpriced, unbounded pre-fee work relative to the actual stake weight the connection should be entitled to.

### Likelihood Explanation
Any unprivileged client that can establish (or briefly hold) stake long enough to open a QUIC TPU connection can exploit this by keeping that single connection alive across stake changes/epoch boundaries; this requires no validator/operator privilege and uses only the standard client-facing QUIC connection path (`spawn_stake_weighted_qos_server` / `spawn_simple_qos_server`), which is explicitly in-scope. The staked_nodes table is refreshed only every 5 seconds and actual epoch stake weights change at most once per epoch, so a connection opened while staked can persist well beyond the point its stake is no longer valid.

### Recommendation
Periodically (or on `staked_nodes` update) re-resolve `peer_type`/`total_stake` for open connections in `ConnectionTable`/`ConnectionEntry`, and either reclassify (adjusting the stream counter/`max_concurrent_uni_streams`) or forcibly close connections whose backing stake has dropped below the threshold used at admission, instead of trusting the value cached in `SwQosConnectionContext`/`SimpleQosConnectionContext` for the connection's entire lifetime.

### Proof of Concept
1. Peer P holds sufficient stake and opens a QUIC TPU connection; `build_connection_context` classifies it `ConnectionPeerType::Staked(stake)` with a proportionally large `total_stake` share, and `cache_new_connection` sets a correspondingly large `max_concurrent_uni_streams` and admits it into the staked `ConnectionTable`.
2. P deactivates/withdraws its stake (or is dropped from the active stake set on the next epoch boundary reflected by `StakedNodesUpdaterService`).
3. P keeps the existing QUIC connection alive using keep-alive pings rather than reconnecting.
4. Because `on_new_stream`/`max_streams_per_throttling_interval` still reference the original cached `SwQosConnectionContext.peer_type`/`total_stake`, P continues to receive the original high-stake throughput allowance and stream concurrency limit indefinitely, bypassing the throttling intended for unstaked/low-stake peers.

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

**File:** streamer/src/nonblocking/swqos.rs (L301-342)
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
    }
```

**File:** streamer/src/nonblocking/quic.rs (L510-532)
```rust
                stats.total_new_connections.fetch_add(1, Ordering::Relaxed);

                let mut conn_context = qos.build_connection_context(&new_connection);
                if let Some(cancel_connection) = qos
                    .try_add_connection(
                        client_connection_tracker,
                        &new_connection,
                        &mut conn_context,
                    )
                    .await
                {
                    tasks.spawn(handle_connection(
                        packet_sender.clone(),
                        from,
                        new_connection,
                        stats,
                        server_params.wait_for_chunk_timeout,
                        server_params.max_stream_data_bytes,
                        conn_context.clone(),
                        qos,
                        cancel_connection,
                    ));
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
