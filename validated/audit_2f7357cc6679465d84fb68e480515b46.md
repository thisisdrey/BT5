### Title
Per-connection stake-based stream quota lets a single staked peer multiply its allocated throughput by opening multiple concurrent connections - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
The `GaugeController` bug is a "resource allocated per-unit without tracking cumulative allocation per identity" flaw: each `vote()` call sets a weight for one gauge without checking the sum across all gauges the same user has voted for, letting a single veToken holder claim far more than 100% of their voting power by spreading votes across gauges. The structurally equivalent pattern exists in agave's stake-weighted QUIC QoS layer (`SwQos`), where the per-stream throughput quota granted to a peer is computed purely from `stake / total_stake` and applied independently to *each connection* that peer opens, with no tracking of how much aggregate quota that pubkey already holds across its other concurrent connections.

### Finding Description
In `SwQos::build_connection_context` and `compute_max_allowed_uni_streams_with_rtt`, a staked peer's throughput allowance is derived solely from its stake proportion of total stake: [1](#0-0) 

This per-connection allowance (`max_uni_streams`, and separately the throttling-window quota from `available_load_capacity_in_throttling_duration`) is computed and applied independently for every new connection via `cache_new_connection`: [2](#0-1) 

The only limiter on the number of concurrent connections a single staked pubkey may hold is `max_connections_per_staked_peer` (`DEFAULT_MAX_QUIC_CONNECTIONS_PER_STAKED_PEER`), enforced by `try_add_connection`/`ConnectionTable`, but this bounds *connection count*, not *cumulative granted throughput*. Each connection gets its own independent `ConnectionStreamCounter` and its own stake-proportional stream quota computed from `conn_context.total_stake` and `conn_context.peer_type` (`Staked(stake)`), exactly as in the vulnerable `vote()` function each gauge got its own independent weight entry keyed by `(msg.sender, gauge)`: [3](#0-2) [4](#0-3) 

The EMA-based aggregate load control (`StakedStreamLoadEMA`) does track total staked load across the whole validator and can globally throttle when saturated, but it only kicks in once overall staked load crosses `staked_throttling_on_load_threshold`; below that global saturation point, per-connection quotas are granted independently per stake ratio with no per-pubkey aggregate cap, so a peer's *effective* share of validator ingress bandwidth scales linearly with the number of connections it opens (up to `max_connections_per_staked_peer`), not with its stake alone.

### Impact Explanation
A staked peer can obtain a multiple of its fair, stake-proportional share of TPU stream ingress bandwidth simply by opening several concurrent QUIC connections instead of one, each independently receiving the full stake-proportional stream quota and QUIC `max_concurrent_uni_streams` setting. This lets a lower-stake but still eligible peer crowd out other staked/unstaked traffic disproportionately to its actual stake, undermining the QoS fairness guarantee the stake-weighted throttle is meant to provide — the direct analog of the gauge-voting bug where a user's total allocated "vote" exceeded their real voting power because per-gauge entries weren't summed against a global cap.

### Likelihood Explanation
This requires no privileged access — any staked peer (unprivileged relative to the validator) can open up to `max_connections_per_staked_peer` connections from the same identity key and is reachable purely over the public QUIC TPU listener, matching the "unprivileged-user analog" scope. It does not require malicious packets, snapshot tampering, or off-limits components.

### Recommendation
Track and cap cumulative granted stream/stream-quota allocation per staked pubkey (not just connection count) across all of that pubkey's open connections — e.g., divide the stake-proportional throttling-window quota by the peer's current open connection count, or maintain a per-pubkey aggregate token bucket shared across all its connections in `SwQos`/`ConnectionTable`, analogous to adding `userTotalGaugeVotes` tracking in the `GaugeController` fix.

### Proof of Concept
Not independently reproduced against a running validator in this analysis; the report is based on static code review of `swqos.rs`/`stream_throttle.rs` showing the quota-computation path is per-connection and stake-ratio based with no aggregation across a peer's simultaneously-open connections. A concrete PoC would require running the streamer stack, establishing `max_connections_per_staked_peer` connections from one staked identity, and observing that aggregate accepted stream throughput for that peer exceeds the single-connection stake-proportional quota by roughly the number of connections opened.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L147-179)
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
}
```

**File:** streamer/src/nonblocking/swqos.rs (L196-232)
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
            debug!(
                "Peer type {:?}, total stake {}, max streams {} from peer {}",
                conn_context.peer_type(),
                conn_context.total_stake,
                max_uni_streams.into_inner(),
                remote_addr,
            );
            Ok((last_update, cancel_connection, stream_counter))
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
