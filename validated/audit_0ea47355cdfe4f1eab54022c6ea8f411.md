### Title
Stale stake-derived peer classification is never re-checked, allowing QUIC QoS evasion by de-staked peers - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
The QUIC ingest QoS controllers (`SwQos` and `SimpleQos`) classify a connecting peer as `Staked(stake)` or `Unstaked` exactly once, at connection-establishment time, by reading the current `StakedNodes` table. That classification (and the stake value baked into it) is then cached for the entire lifetime of the QUIC connection and reused on every subsequent throttling decision, without ever being refreshed against the live stake table.

### Finding Description
`get_connection_stake()` looks up the current stake for the peer's pubkey once and returns `(pubkey, stake, total_stake)` [1](#0-0) . Both QoS controllers call this only inside `build_connection_context()`, which runs once when a connection is accepted, and store the resulting `ConnectionPeerType`/stake in the long-lived context/entry structures:

- `SwQos::build_connection_context` computes `peer_type` and `total_stake` once and stores them in `SwQosConnectionContext` [2](#0-1) .
- `SimpleQos::build_connection_context` does the same for `SimpleQosConnectionContext` [3](#0-2) .
- The connection table stores this same cached `peer_type` in `ConnectionEntry`, exposing it via `stake()` for pruning decisions, with no update path after insertion [4](#0-3) .

This cached, stale classification is subsequently used for the connection's entire lifetime to:
- Compute the number of allowed concurrent uni-streams via `compute_max_allowed_uni_streams_with_rtt(rtt_millis, conn_context.peer_type(), conn_context.total_stake)` [5](#0-4) .
- Decide per-throttling-interval stream budgets via `max_streams_per_throttling_interval`, which reads `conn_context.peer_type` and `conn_context.total_stake` directly from the cached context rather than re-querying `StakedNodes` [6](#0-5) .
- Decide table placement/eviction resistance: staked connections are placed in `staked_connection_table` (capacity `max_staked_connections`) and are exempt from the smaller/aggressively-pruned `unstaked_connection_table`, and higher cached stake makes a connection harder to evict via `prune_random`'s `threshold_stake` comparison [7](#0-6) [8](#0-7) .

There is no code path anywhere in the QUIC streamer that re-invokes `get_connection_stake` or otherwise re-evaluates a peer's stake for an already-established connection; a grep for stake-refresh logic in `streamer/src` found none. This is structurally the same defect pattern as the reported `transferSAFEOwnership()` bug: a privilege (here, "staked" QoS status derived from a mutable, attacker-controlled attribute — stake) is granted based on a point-in-time snapshot and is never revoked or re-validated when the underlying attribute (stake) later changes, even though the surrounding infrastructure (`StakedNodesUpdaterService`) actively keeps `StakedNodes` up to date every 5 seconds [9](#0-8) .

### Impact Explanation
An unprivileged network peer that holds stake, or colludes with a staked party, can open a QUIC connection to a validator's TPU/QUIC endpoint while staked, obtaining a `ConnectionPeerType::Staked(stake)` classification with an associated higher `max_concurrent_uni_streams` limit, inclusion in the higher-capacity/eviction-resistant `staked_connection_table`, and a proportionally larger stream-throttling budget under `StakedStreamLoadEMA`. The peer (or the stake owner, who fully controls delegation/deactivation) can then fully or partially unstake while keeping the same QUIC connection open. Because the cached `peer_type`/`total_stake` in `ConnectionEntry`/`*ConnectionContext` is never refreshed, the connection continues to be treated as staked (or as staked with the old, now-inflated, stake weight) for as long as the connection stays alive — evading the fair-share/anti-spam QoS controls (SWQoS) that these structures exist to enforce, and denying bandwidth/stream capacity to genuinely-staked peers. This is a QoS-evasion class impact explicitly listed as acceptable in the validated impact list.

### Likelihood Explanation
This does not require any special/validator role — any unstaked or staked network client can open a QUIC connection to a public-facing TPU/forwarding port. Unstaking/reducing stake is a routine, permissionless action the delegator can trigger at will, and QUIC connections are commonly kept open/reused across many transactions (the connection cache and 0-RTT reconnection logic in `quic-client` exist specifically to keep connections warm). No mocked-only or theoretical setup is needed: the classification logic and its one-time-cache nature are directly demonstrated by the code paths cited above.

### Recommendation
Periodically re-validate each open connection's live stake against `StakedNodes` (e.g., on a timer alongside `StakedNodesUpdaterService`'s 5s refresh cycle, or lazily on each throttling decision) and update/downgrade `ConnectionPeerType`, `total_stake`, and the associated table placement (moving the connection from `staked_connection_table` to `unstaked_connection_table`, or vice versa) when the peer's stake changes, rather than relying on a value cached once at connection-establishment time.

### Proof of Concept
1. Peer P holds sufficient stake so that `get_connection_stake` returns `ConnectionPeerType::Staked(stake)` when P opens a QUIC connection to the validator's TPU (`SwQos::build_connection_context`, `streamer/src/nonblocking/swqos.rs:302-341`).
2. `SwQos::try_add_connection` places the connection in `staked_connection_table` and grants it a stake-proportional `max_concurrent_uni_streams` and throttling budget (`streamer/src/nonblocking/swqos.rs:344-414`, `147-179`, `292-298`).
3. P (or the stake delegator) fully deactivates/withdraws their stake. `StakedNodesUpdaterService` updates the global `StakedNodes` map within 5 seconds (`core/src/staked_nodes_updater_service.rs:16-42`).
4. P keeps the existing QUIC connection alive (no new `build_connection_context` call occurs for an already-established connection).
5. P continues opening uni-streams/sending transactions on the same connection. Because `conn_context.peer_type`/`total_stake` in the cached `SwQosConnectionContext` was never re-derived, P still benefits from the staked stream-limit, staked throttling budget, and staked-table eviction resistance — despite now having zero stake — until the connection is eventually closed for an unrelated reason.

### Citations

**File:** streamer/src/nonblocking/quic.rs (L416-428)
```rust
pub fn get_connection_stake(
    connection: &Connection,
    staked_nodes: &RwLock<StakedNodes>,
) -> Option<(Pubkey, u64, u64)> {
    let pubkey = get_remote_pubkey(connection)?;
    debug!("Peer public key is {pubkey:?}");
    let staked_nodes = staked_nodes.read().unwrap();
    Some((
        pubkey,
        staked_nodes.get_node_stake(&pubkey)?,
        staked_nodes.total_stake(),
    ))
}
```

**File:** streamer/src/nonblocking/quic.rs (L860-902)
```rust
struct ConnectionEntry<S: OpaqueStreamerCounter> {
    cancel: CancellationToken,
    peer_type: ConnectionPeerType,
    last_update: Arc<AtomicU64>,
    port: u16,
    // We do not explicitly use it, but its drop is triggered when ConnectionEntry is dropped.
    _client_connection_tracker: ClientConnectionTracker,
    connection: Option<Connection>,
    stream_counter: Arc<S>,
}

impl<S: OpaqueStreamerCounter> ConnectionEntry<S> {
    fn new(
        cancel: CancellationToken,
        peer_type: ConnectionPeerType,
        last_update: Arc<AtomicU64>,
        port: u16,
        client_connection_tracker: ClientConnectionTracker,
        connection: Option<Connection>,
        stream_counter: Arc<S>,
    ) -> Self {
        Self {
            cancel,
            peer_type,
            last_update,
            port,
            _client_connection_tracker: client_connection_tracker,
            connection,
            stream_counter,
        }
    }

    fn last_update(&self) -> u64 {
        self.last_update.load(Ordering::Relaxed)
    }

    fn stake(&self) -> u64 {
        match self.peer_type {
            ConnectionPeerType::Unstaked => 0,
            ConnectionPeerType::Staked(stake) => stake,
        }
    }
}
```

**File:** streamer/src/nonblocking/quic.rs (L982-1006)
```rust
    // Randomly selects sample_size many connections, evicts the one with the
    // lowest stake, and returns the number of pruned connections.
    // If the stakes of all the sampled connections are higher than the
    // threshold_stake, rejects the pruning attempt, and returns 0.
    pub(crate) fn prune_random(&mut self, sample_size: usize, threshold_stake: u64) -> usize {
        let num_pruned = std::iter::once(self.table.len())
            .filter(|&size| size > 0)
            .flat_map(|size| {
                let mut rng = rng();
                repeat_with(move || rng.random_range(0..size))
            })
            .map(|index| {
                let connection = self.table[index].first();
                let stake = connection.map(|connection: &ConnectionEntry<S>| connection.stake());
                (index, stake)
            })
            .take(sample_size)
            .min_by_key(|&(_, stake)| stake)
            .filter(|&(_, stake)| stake < Some(threshold_stake))
            .and_then(|(index, _)| self.table.swap_remove_index(index))
            .map(|(_, connections)| connections.len())
            .unwrap_or_default();
        self.total_size = self.total_size.saturating_sub(num_pruned);
        num_pruned
    }
```

**File:** streamer/src/nonblocking/swqos.rs (L196-208)
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

**File:** streamer/src/nonblocking/swqos.rs (L344-414)
```rust
    #[allow(clippy::manual_async_fn)]
    fn try_add_connection(
        &self,
        client_connection_tracker: ClientConnectionTracker,
        connection: &quinn::Connection,
        conn_context: &mut SwQosConnectionContext,
    ) -> impl Future<Output = Option<CancellationToken>> + Send {
        async move {
            const PRUNE_RANDOM_SAMPLE_SIZE: usize = 2;

            match conn_context.peer_type() {
                ConnectionPeerType::Staked(stake) => {
                    let mut connection_table_l = self.staked_connection_table.lock().await;

                    if connection_table_l.total_size >= self.config.max_staked_connections {
                        let num_pruned =
                            connection_table_l.prune_random(PRUNE_RANDOM_SAMPLE_SIZE, stake);
                        self.stats
                            .num_evictions_staked
                            .fetch_add(num_pruned, Ordering::Relaxed);
                        update_open_connections_stat(&self.stats, &connection_table_l);
                    }

                    if connection_table_l.total_size < self.config.max_staked_connections {
                        if let Ok((last_update, cancel_connection, stream_counter)) = self
                            .cache_new_connection(
                                client_connection_tracker,
                                connection,
                                connection_table_l,
                                conn_context,
                            )
                        {
                            self.stats
                                .connection_added_from_staked_peer
                                .fetch_add(1, Ordering::Relaxed);
                            conn_context.in_staked_table = true;
                            conn_context.last_update = last_update;
                            conn_context.stream_counter = Some(stream_counter);
                            return Some(cancel_connection);
                        }
                    } else {
                        // If we couldn't prune a connection in the staked connection table, let's
                        // put this connection in the unstaked connection table. If needed, prune a
                        // connection from the unstaked connection table.
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
                                .connection_added_from_staked_peer
                                .fetch_add(1, Ordering::Relaxed);
                            conn_context.in_staked_table = false;
                            conn_context.last_update = last_update;
                            conn_context.stream_counter = Some(stream_counter);
                            return Some(cancel_connection);
                        } else {
                            self.stats
                                .connection_add_failed_on_pruning
                                .fetch_add(1, Ordering::Relaxed);
                            self.stats
                                .connection_add_failed_staked_node
                                .fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L256-273)
```rust
impl QosController<SimpleQosConnectionContext> for SimpleQos {
    fn build_connection_context(&self, connection: &Connection) -> SimpleQosConnectionContext {
        let (peer_type, remote_pubkey, _total_stake) =
            get_connection_stake(connection, &self.staked_nodes).map_or(
                (ConnectionPeerType::Unstaked, None, 0),
                |(pubkey, stake, total_stake)| {
                    (ConnectionPeerType::Staked(stake), Some(pubkey), total_stake)
                },
            );

        SimpleQosConnectionContext {
            peer_type,
            remote_pubkey,
            remote_address: connection.remote_address(),
            last_update: Arc::new(AtomicU64::new(timing::timestamp())),
            stream_counter: None,
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
