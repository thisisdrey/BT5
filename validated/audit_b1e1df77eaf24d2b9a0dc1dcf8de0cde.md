### Title
Stale Per-Connection Stake Snapshot Lets Reduced-Stake Peers Retain Elevated QUIC QoS Privileges - (File: `streamer/src/nonblocking/swqos.rs`)

### Summary
`SwQos::build_connection_context()` snapshots a peer's stake and derives its `ConnectionPeerType`/`total_stake` exactly once, at QUIC connection establishment. That snapshot is cached for the entire lifetime of the connection and used to set `max_concurrent_uni_streams` and to compute the peer's per-throttling-interval stream budget on every subsequent stream, even though the authoritative `staked_nodes` table is refreshed independently every 5 seconds. If a peer's real stake later drops (e.g., after unstaking/deactivation) below what was captured at connect time, the long-lived connection keeps enjoying the old, higher QoS allowance because the cached context is never re-derived. This mirrors the audited GTE-perps bug: a privileged limit (`maxOpenLeverage`) is enforced only at the moment a value is *set*, but a later-computed action (`getPositionLeverage` / here, per-stream throttling) reuses the stale cached value instead of re-checking against the live, reduced limit.

### Finding Description
`get_connection_stake()` reads the current stake for a peer from the shared `staked_nodes: Arc<RwLock<StakedNodes>>` at connection setup time: [1](#0-0) 

`SwQos::build_connection_context()` calls this once per new `Connection` and computes `peer_type` (`Staked(stake)` vs `Unstaked`) and `total_stake`, storing them into `SwQosConnectionContext`: [2](#0-1) 

This context is then used in `cache_new_connection()` to compute `max_uni_streams` via `compute_max_allowed_uni_streams_with_rtt(rtt_millis, conn_context.peer_type(), conn_context.total_stake)`, and this value is pushed to the QUIC transport once with `connection.set_max_concurrent_uni_streams(max_uni_streams)`: [3](#0-2) 

The same cached `conn_context.peer_type`/`total_stake` fields are reused for every stream-admission decision for the life of the connection via `max_streams_per_throttling_interval()`: [4](#0-3) 

Meanwhile, the ground-truth stake table used by `get_connection_stake` is refreshed independently on a fixed cadence by a background service, completely decoupled from individual connection lifetimes: [5](#0-4) 

Because `SwQosConnectionContext` is built once and never rebuilt for an existing `Connection`, there is no mechanism analogous to `MarketLib.assertMaxLeverage()` that re-validates the peer's *current* stake against the *current* `staked_nodes` table before granting throughput. A peer whose stake was high at connection time keeps its high-stake QUIC uni-stream budget and higher stream-throttling allowance (`Staked(stake)`) indefinitely, as long as the connection stays open, regardless of subsequent reductions in that peer's real stake recorded in `staked_nodes`.

### Impact Explanation
This allows a peer to retain a stake-weighted QoS advantage (larger `max_concurrent_uni_streams`, higher share of the staked stream-throttling budget) after their entitlement to that advantage has shrunk, as long as they keep the original QUIC connection alive. This is a quality-of-service/anti-DoS control bypass: it lets a peer consume network/validator-forwarding resources disproportionate to their live stake, degrading fairness of the stake-weighted QoS system and undermining its intended anti-spam/anti-DoS purpose for other (including newly higher-staked) peers. It does not require any special validator/operator role — any staked peer that later reduces its stake (or is slashed/deactivated in stake terms) and keeps its connection open benefits from this stale privilege, matching the report's "legacy high privilege persists after admin/config reduction" pattern.

### Likelihood Explanation
Likelihood is moderate-to-high: QUIC connections used by validators for transaction forwarding are commonly kept alive across multiple slots/epochs rather than being torn down every 5-second stake-refresh cycle, and nothing in `try_add_connection`/`cache_new_connection` re-derives or re-applies `SwQosConnectionContext` mid-life. Any staked peer whose stake decreases (voluntarily or otherwise) while holding an open connection will trivially retain the stale, more favorable QoS treatment without any special effort.

### Recommendation
Periodically re-derive (or at minimum re-check) `peer_type`/`total_stake` for existing connections against the live `staked_nodes` table (e.g., on each stream-admission decision or on a timer tied to `STAKE_REFRESH_CYCLE`), and re-apply `set_max_concurrent_uni_streams` and the throttling peer type when the peer's stake has changed materially. At minimum, cap the staked benefit duration so long-lived connections cannot outlive multiple stake-refresh cycles without revalidation, mirroring the report's recommendation to only ever allow a reduction in privilege, never let a stale/higher cached value silently persist.

### Proof of Concept
1. Peer `A` connects while staked with `stake = S_high`; `build_connection_context` sets `peer_type = Staked(S_high)`, and `cache_new_connection` sets `max_concurrent_uni_streams` based on `S_high`.
2. `A` keeps the QUIC connection open and continues sending streams.
3. Independently, `A`'s real stake decreases to `S_low` (e.g., due to unstaking), reflected in `staked_nodes` after the next `STAKE_REFRESH_CYCLE` (5s) tick in `StakedNodesUpdaterService::new`.
4. `SwQosConnectionContext` for `A`'s existing connection is never rebuilt; `max_streams_per_throttling_interval` and the QUIC-level `max_concurrent_uni_streams` continue to reflect `S_high`.
5. `A` continues to receive the higher stream budget/quota than a fresh connection from a peer with the same (now-current) `S_low` stake would receive, demonstrating the bypass of the intended, updated stake-weighted QoS limit.

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

**File:** streamer/src/nonblocking/swqos.rs (L181-232)
```rust
impl SwQos {
    fn cache_new_connection(
        &self,
        client_connection_tracker: ClientConnectionTracker,
        connection: &Connection,
        mut connection_table_l: MutexGuard<ConnectionTable<ConnectionStreamCounter>>,
        conn_context: &SwQosConnectionContext,
    ) -> Result<
        (
            Arc<AtomicU64>,
            CancellationToken,
            Arc<ConnectionStreamCounter>,
        ),
        ConnectionHandlerError,
    > {
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
            debug!(
                "Peer type {:?}, total stake {}, max streams {} from peer {}",
                conn_context.peer_type(),
                conn_context.total_stake,
                max_uni_streams.into_inner(),
                remote_addr,
            );
            Ok((last_update, cancel_connection, stream_counter))
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

**File:** core/src/staked_nodes_updater_service.rs (L16-45)
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

        Self { thread_hdl }
    }
```
