### Title
Stale stake/peer-type cached at QUIC connection accept time is never rechecked, allowing continued high-priority QoS after stake decreases - (File: streamer/src/nonblocking/swqos.rs)

### Summary
The external report's bug class is a stale value captured at "check" time being reused later for an action ("act"), so the action operates on an outdated state (here: validator balance vs. withdrawal). The closest reachable analog in agave is in the QUIC Stake-Weighted QoS (SWQoS) path: a connection's stake and derived `peer_type`/`total_stake` are computed once, at connection-accept time via `SwQos::build_connection_context()`, and cached in `SwQosConnectionContext`. That cached value is then used for the entire lifetime of the (long-lived, keep-alive) connection to make QoS decisions — max concurrent uni-streams, per-peer connection limits, and per-throttling-interval stream budgets — without ever being refreshed against the current epoch's stake table.

### Finding Description
`SwQos::build_connection_context()` looks up the peer's stake via `get_connection_stake()` (which reads the current `StakedNodes` snapshot) exactly once, when the QUIC connection is accepted: [1](#0-0) [2](#0-1) 

The resulting `peer_type` and `total_stake` are stored inside `SwQosConnectionContext`, a struct that is cached alongside the connection for its entire lifetime: [3](#0-2) 

That cached, one-time-computed context is subsequently used to:
- compute `max_uni_streams` for the connection at accept time (`cache_new_connection`): [4](#0-3) 
- decide per-peer connection limits based on `conn_context.peer_type()` (`ConnectionPeerType::Staked` vs `Unstaked`): [5](#0-4) 
- compute ongoing per-throttling-interval stream budgets for the life of the connection: [6](#0-5) 

Meanwhile, the cluster-wide stake snapshot (`StakedNodes`) used by `get_connection_stake()` is refreshed only periodically in the background (every 5 seconds) by `StakedNodesUpdaterService`, independent of any individual connection's context: [7](#0-6) 

Because `SwQosConnectionContext` is built once per connection and never rebuilt for the QUIC idle-timeout duration (`QUIC_MAX_TIMEOUT` = 30s, extendable indefinitely via keep-alive traffic on an already-open connection), a validator/staker whose delegated stake decreases or is fully undelegated after connection establishment continues to be treated as "Staked" with the original (now-stale) stake amount — receiving elevated `max_uni_streams`, exemption from the strict unstaked connection cap, and preferential throttling-interval stream budgets — until the connection is torn down and re-established. [8](#0-7) 

This mirrors the report's root cause pattern precisely: a balance/stake value is captured at one point in time and used to gate a resource-allocation decision at a later, unbounded point in time, with no mechanism to refresh it to reflect the current, correct state.

### Impact Explanation
This is a QoS-evasion vector explicitly listed as an accepted impact category. An attacker who briefly acquires stake (or colludes with a staked party) to open a connection while staked, then has that stake removed/redelegated, keeps consuming the elevated staked-tier QUIC resources (higher `max_uni_streams`, exemption from unstaked connection/stream caps, and higher throughput priority via `staked_stream_load_ema`) for as long as the connection is kept alive — degrading fairness of the TPU QUIC ingress path and letting effectively-unstaked traffic crowd out legitimately staked or unstaked peers.

### Likelihood Explanation
Medium. It requires only unprivileged, permissionless actions: establish a QUIC connection while stake is present, then have the stake removed (a state fully controllable by an ordinary user delegating/undelegating), and keep the connection alive via ordinary traffic/keep-alives. No validator/operator privilege is needed to exploit it — only control over one's own stake account and network client, which are unprivileged-user actions matching the scope constraints.

### Recommendation
Periodically re-validate (or re-derive) `SwQosConnectionContext.peer_type`/`total_stake` against the latest `StakedNodes` snapshot for long-lived connections (e.g., on each stream-throttling interval reset or on a timer tied to `STAKE_REFRESH_CYCLE`), and downgrade a connection from staked to unstaked treatment (re-applying unstaked connection/stream limits) once its underlying stake drops below the staked threshold, rather than trusting the value captured at connection-accept time for the connection's entire lifetime.

### Proof of Concept
1. Delegate stake to a validator identity key and use that keypair to open a QUIC TPU connection to a target agave node; `get_connection_stake()` returns a nonzero stake, and `build_connection_context()` classifies the connection as `ConnectionPeerType::Staked(stake)` with the current `total_stake`, cached in `SwQosConnectionContext`.
2. Immediately undelegate/deactivate the stake so the account's stake becomes 0 in the next `StakedNodes` refresh (within `STAKE_REFRESH_CYCLE` = 5s, per `core/src/staked_nodes_updater_service.rs`).
3. Keep the already-open QUIC connection alive using periodic traffic (well under `QUIC_MAX_TIMEOUT` = 30s idle timeout).
4. Continue sending streams on the same connection: `SwQos::max_streams_per_throttling_interval()` and per-peer connection-limit checks keep using the stale cached `conn_context.total_stake`/`peer_type`, so the connection continues to receive staked-tier QoS treatment (elevated `max_uni_streams`, exemption from unstaked caps) even though the sender is now effectively unstaked, since no code path in `streamer/src/nonblocking/swqos.rs` re-derives the context from a fresh `StakedNodes` lookup for the life of the connection.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L97-107)
```rust
// QoS Params for Stake weighted QoS
#[derive(Clone)]
pub struct SwQosConnectionContext {
    peer_type: ConnectionPeerType,
    remote_pubkey: Option<solana_pubkey::Pubkey>,
    total_stake: u64,
    in_staked_table: bool,
    last_update: Arc<AtomicU64>,
    remote_address: std::net::SocketAddr,
    stream_counter: Option<Arc<ConnectionStreamCounter>>,
}
```

**File:** streamer/src/nonblocking/swqos.rs (L196-203)
```rust
        // get current RTT and limit it to MAX_RTT_MS right away
        let rtt_millis = connection.rtt().as_millis().min(MAX_RTT_MS as u128) as u32;
        let max_uni_streams = VarInt::from_u32(compute_max_allowed_uni_streams_with_rtt(
            rtt_millis,
            conn_context.peer_type(),
            conn_context.total_stake,
        ));
        let remote_addr = conn_context.remote_address;
```

**File:** streamer/src/nonblocking/swqos.rs (L205-208)
```rust
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

**File:** streamer/src/quic.rs (L36-38)
```rust
/// QUIC connection idle timeout. The connection will be closed if there are no activities on it
/// within the timeout window. The chosen value is default for quinn.
pub const QUIC_MAX_TIMEOUT: Duration = Duration::from_secs(30);
```
