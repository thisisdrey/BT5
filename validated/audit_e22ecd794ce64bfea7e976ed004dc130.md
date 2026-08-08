### Title
Stale per-connection `total_stake` snapshot lets long-lived QUIC connections retain an oversized share of the staked stream budget - (File: `streamer/src/nonblocking/swqos.rs`)

### Summary
`SwQos` computes each staked connection's allowed streams-per-interval as `stake / total_stake` of a fixed staked-stream budget, but `total_stake` is captured once at connection setup and never refreshed for the life of the connection. As cluster stake shifts, old connections keep computing their share against a stale denominator, so the sum of per-connection quotas can drift away from (and exceed) the aggregate budget the mechanism is designed to enforce — the same "stale shared-denominator" root cause as the referenced `TributeAccrual` conviction-score bug.

### Finding Description
`SwQos::build_connection_context` snapshots `total_stake` from `StakedNodes` at the moment a connection is accepted and stores it in `SwQosConnectionContext::total_stake`, which is never updated afterward: [1](#0-0) 

This stored, immutable `total_stake` is subsequently used for the entire lifetime of the connection to compute the connection's allowed stream rate: [2](#0-1) 

The actual quota calculation is a straight proportional split of a fixed, precomputed budget (`max_staked_load_in_throttling_window`, set once in `StakedStreamLoadEMA::new` and never recomputed) by `stake / total_stake`: [3](#0-2) [4](#0-3) 

`total_stake` itself is derived from the gossip-driven `StakedNodes` map (or admin-supplied `staked-nodes-overrides`), which changes over time (epoch boundaries, new/removed validators, or `set_staked_nodes_overrides` admin calls): [5](#0-4) [6](#0-5) 

This is structurally identical to the reported bug class: a per-actor share is computed as `actor_value / shared_total`, but different actors (here, different QUIC connections) evaluate this ratio against different snapshots of `shared_total` because updates to the shared value are not synchronized across all outstanding claims/allocations. Just as Bob's earlier `updateConvictionScore()` call let him claim a disproportionate share against a smaller `totalCS`, a validator (or forwarding peer) that opens a QUIC connection while `total_stake` is small (e.g., right before other validators' stake becomes visible, or right after some validators drop off the stake table) locks in a permanently inflated `stake/total_stake` ratio for as long as that connection stays open — QUIC connections are long-lived and are only re-evaluated on reconnect via `remove_connection`/`try_add_connection`, not on stake-table refresh.

### Impact Explanation
Because `max_staked_load_in_throttling_window` is a fixed aggregate budget meant to be split proportionally across all currently-staked connections, and each connection independently enforces its own quota via its own `ConnectionStreamCounter` (no shared/global token pool draws down as connections consume streams), the invariant `sum(stake_i/total_stake_i) <= 1` that the design implicitly relies on can be violated when connections carry different, stale `total_stake_i` values. This lets long-lived connections retain a larger proportional stream allowance than the current stake distribution justifies, at the expense of fair stake-weighted allocation among staked peers — a QoS evasion: it undermines the intended stake-weighted fairness/anti-spam guarantee of the QUIC TPU ingestion path without requiring any privileged action, merely persistence of a connection across stake changes.

### Likelihood Explanation
Stake maps used by `StakedNodes` change routinely (validators joining/leaving, delegation changes across epochs, or an operator applying `staked-nodes-overrides`). Any staked peer with a long-lived QUIC connection is, by design, unaffected by these updates for its already-established connection, so the condition arises passively during normal cluster operation and does not require an attacker to do anything unusual beyond keeping a connection open — increasing the practical likelihood of at least mild, persistent unfairness.

### Recommendation
Periodically refresh `conn_context.total_stake` (and re-derive `peer_type`/quota) for active connections when the underlying `StakedNodes` table changes, or recompute `available_load_capacity_in_throttling_duration` using a shared, live `total_stake` reference (e.g., an `Arc<AtomicU64>` updated whenever `StakedNodes` is swapped) instead of a value captured once at connection-accept time.

### Proof of Concept
1. Validator A's connection is accepted while `StakedNodes::total_stake()` = `T1`; its `SwQosConnectionContext.total_stake` is fixed to `T1` per `build_connection_context` (`streamer/src/nonblocking/swqos.rs:301-341`).
2. Time passes; the network's stake table grows (new validators stake, or existing large stakers add stake), so `StakedNodes::total_stake()` becomes `T2 > T1`, updated via `StakedNodes::new`/`calculate_total_stake` (`streamer/src/streamer.rs:408-428`) whenever the node refreshes `staked_nodes` from gossip.
3. All new connections computed after the refresh use `T2` as the denominator in `available_load_capacity_in_throttling_duration` (`streamer/src/nonblocking/stream_throttle.rs:167-188`), giving them a smaller proportional share for the same stake.
4. Validator A's still-open connection continues to use `T1` via its cached `conn_context.total_stake` (`streamer/src/nonblocking/swqos.rs:292-298`), retaining a stream quota calculated against the old, smaller total — a persistently larger proportional allocation than currently-connecting peers with identical stake receive, and one that is no longer consistent with the aggregate `max_staked_load_in_throttling_window` budget the mechanism was designed to bound.

### Citations

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

**File:** streamer/src/nonblocking/stream_throttle.rs (L46-62)
```rust
impl StakedStreamLoadEMA {
    pub(crate) fn new(
        stats: Arc<StreamerStats>,
        max_unstaked_connections: usize,
        max_streams_per_ms: u64,
    ) -> Self {
        let allow_unstaked_streams = max_unstaked_connections > 0;
        let max_staked_load_in_ms = if allow_unstaked_streams {
            max_streams_per_ms
                - ((EXPECTED_UNSTAKED_STREAMS_RATIO * (max_streams_per_ms as f64)) as u64)
        } else {
            max_streams_per_ms
        };

        let max_staked_load_in_ema_interval = max_staked_load_in_ms * STREAM_LOAD_EMA_INTERVAL_MS;
        let max_staked_load_in_throttling_window =
            max_staked_load_in_ms * STREAM_THROTTLING_INTERVAL_MS;
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

**File:** streamer/src/streamer.rs (L408-428)
```rust
impl StakedNodes {
    fn calculate_total_stake(
        stakes: &HashMap<Pubkey, u64>,
        overrides: &HashMap<Pubkey, u64>,
    ) -> u64 {
        stakes
            .iter()
            .filter(|(pubkey, _)| !overrides.contains_key(pubkey))
            .map(|(_, &stake)| stake)
            .chain(overrides.values().copied())
            .sum()
    }

    pub fn new(stakes: Arc<HashMap<Pubkey, u64>>, overrides: HashMap<Pubkey, u64>) -> Self {
        let total_stake = Self::calculate_total_stake(&stakes, &overrides);
        Self {
            stakes,
            overrides,
            total_stake,
        }
    }
```

**File:** validator/src/commands/staked_nodes_overrides/mod.rs (L47-59)
```rust
pub fn execute(matches: &ArgMatches, ledger_path: &Path) -> Result<()> {
    let staked_nodes_overrides_args = StakedNodesOverridesArgs::from_clap_arg_match(matches)?;

    let admin_client = admin_rpc_service::connect(ledger_path);
    admin_rpc_service::runtime().block_on(async move {
        admin_client
            .await?
            .set_staked_nodes_overrides(staked_nodes_overrides_args.path)
            .await
    })?;

    Ok(())
}
```
