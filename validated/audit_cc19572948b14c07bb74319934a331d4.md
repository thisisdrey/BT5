### Title
Per-connection stake-weighted stream quota is not normalized across a peer's multiple connections, allowing multiplicative QUIC throughput evasion - (File: streamer/src/nonblocking/swqos.rs)

### Summary
The Ajna report describes a quadratic-voting tally that fails to enforce an aggregate constraint (`sum of squares ≤ stake²`) across a voter's total contributions, letting an actor split influence across many small allocations to exceed its fair proportional share. The analogous flaw in agave's `SwQos` (stake-weighted QUIC QoS) is that the per-connection stream allowance is computed independently for each connection a staked peer opens, using that peer's *raw* stake fraction, without dividing by or otherwise capping the *number of concurrent connections* the same staked identity is permitted to hold. Since a single staked pubkey is allowed up to `max_connections_per_staked_peer` (16 by default) simultaneous connections, and each one independently receives the full stake-proportional stream quota, the peer's effective aggregate throughput scales linearly (up to 16x) with the number of connections it opens rather than being bounded by its stake share — mirroring the "sum without normalization" flaw in the source report.

### Finding Description
`compute_max_allowed_uni_streams_with_rtt` computes a QUIC `max_concurrent_uni_streams` value purely from `(peer_stake / total_stake)`, with no adjustment for how many connections that same pubkey already holds: [1](#0-0) 

This value is set independently on every new connection in `cache_new_connection`: [2](#0-1) 

Similarly, the per-throttling-interval stream cap used by `throttle_stream` is derived from `available_load_capacity_in_throttling_duration(peer_type, total_stake)`, which again is purely a function of `stake`/`total_stake`, not connection count: [3](#0-2) [4](#0-3) [5](#0-4) 

A single staked identity is explicitly allowed multiple simultaneous connections via `max_connections_per_staked_peer` (default 16): [6](#0-5) [7](#0-6) 

Because each of those up-to-16 connections independently gets the *same* stake-proportional `max_concurrent_uni_streams` and the *same* `max_streams_per_throttling_interval`, a staked entity's total admitted stream/packet throughput multiplies with the number of connections it opens, rather than the design intent that a peer's total QoS share be bounded by its share of total stake. This is the direct structural analog of the Ajna bug: instead of squaring/aggregating a bounded quantity before enforcing the cap, the implementation applies the same per-unit allowance repeatedly to each of the actor's "votes" (here, connections), letting the actor exceed its proportionally fair resource share by splitting activity across multiple channels — exactly as Alice bribing many small voters exceeds her fair quadratic share of funding power.

### Impact Explanation
A staked validator/forwarder (any node appearing in `staked_nodes`, which is populated from the stake-weighted gossip table and thus reachable by any staked entity, not just privileged operators) can open up to `max_connections_per_staked_peer` connections from a single machine/pubkey and receive up to that many multiples of its intended per-stake stream quota and QUIC-level concurrent-stream allowance. This lets a modest-stake peer consume a disproportionate share of the leader's/TPU's ingestion capacity (`QUIC_TOTAL_STAKED_CONCURRENT_STREAMS` budget and the EMA-based staked throttling window), degrading fairness of transaction ingestion and potentially starving other staked peers with comparable or larger stake — a QoS evasion of the stake-weighted admission design.

### Likelihood Explanation
Exploitation only requires establishing multiple concurrent QUIC connections to the target's TPU port using the same staked identity/pubkey — well within reach of any unprivileged staked participant, requiring no special access, timing, or client coordination, and is bounded by config constants already present in the codebase (`max_connections_per_staked_peer`, `max_streams_per_ms`), so the multiplier (up to ~16x) is deterministic and easy to realize.

### Recommendation
Normalize the per-connection stream allowance and throttling budget by the number of active connections the same staked pubkey currently holds (e.g., divide the stake-proportional quota by `min(open_connections_for_pubkey, max_connections_per_staked_peer)`), or track and enforce an aggregate per-pubkey stream budget across all of a peer's connections rather than granting the full stake-proportional allowance to each connection independently.

### Proof of Concept
Not independently executed; reasoning is based on static analysis of `compute_max_allowed_uni_streams_with_rtt`, `cache_new_connection`, and `available_load_capacity_in_throttling_duration` in `streamer/src/nonblocking/swqos.rs` and `streamer/src/nonblocking/stream_throttle.rs`, which show the per-connection quota calculation has no dependency on the number of connections concurrently held by the same pubkey, combined with `max_connections_per_staked_peer` explicitly permitting multiple such connections per identity.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L42-46)
```rust
// Set the maximum concurrent stream numbers to avoid excessive streams.
// The value was lowered from 2048 to reduce contention of the limited
// receive_window among the streams which is observed in CI bench-tests with
// forwarded packets from staked nodes.
pub const QUIC_MAX_STAKED_CONCURRENT_STREAMS: u32 = 512;
```

**File:** streamer/src/nonblocking/swqos.rs (L147-178)
```rust
fn compute_max_allowed_uni_streams_with_rtt(
    rtt_millis: u32,
    peer_type: ConnectionPeerType,
    total_stake: u64,
) -> u32 {
    let streams = match peer_type {
        ConnectionPeerType::Staked(peer_stake) => {
            // No checked math for f64 type. So let's explicitly check for 0 here
            if total_stake == 0 || peer_stake > total_stake {
                warn!(
                    "Invalid stake values: peer_stake: {peer_stake:?}, total_stake: \
                     {total_stake:?}"
                );

                QUIC_MIN_STAKED_CONCURRENT_STREAMS
            } else {
                let delta = (QUIC_TOTAL_STAKED_CONCURRENT_STREAMS
                    - QUIC_MIN_STAKED_CONCURRENT_STREAMS) as f64;

                (((peer_stake as f64 / total_stake as f64) * delta) as u32
                    + QUIC_MIN_STAKED_CONCURRENT_STREAMS)
                    .clamp(
                        QUIC_MIN_STAKED_CONCURRENT_STREAMS,
                        QUIC_MAX_STAKED_CONCURRENT_STREAMS,
                    )
            }
        }
        ConnectionPeerType::Unstaked => QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
    };
    // scale amount of streams based on RTT if RTT is larger than REFERENCE_RTT_MS
    // multiply first then divide to avoid rounding errors.
    (streams.saturating_mul(rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS))) / REFERENCE_RTT_MS
```

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
