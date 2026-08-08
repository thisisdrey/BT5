### Title
Stake-Weighted QUIC QoS stream allocation is computed from an instantaneous, peer-influenceable RTT sample, enabling QoS evasion - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
The reported bug class is "use of an instantaneous, manipulable measurement as direct input to a critical allocation/valuation calculation, without any averaging/verification, enabling gaming by the party who controls that measurement." The `OmoOracle` uses Uniswap's `slot0()` spot price with no TWAP to size a position's value. The closest reachable analog in Agave's unprivileged-facing surface is the QUIC stake-weighted QoS controller, which uses a single, instantaneous `connection.rtt()` sample — a value a remote peer can influence — to directly scale the number of concurrent streams granted to a connection, with no smoothing/verification against the peer's actual, legitimately-earned stake-based allocation.

### Finding Description
`compute_max_allowed_uni_streams_with_rtt()` computes the number of QUIC uni-directional streams a connection is allowed, first from stake ratio, then multiplies the result by a "BDP scaling" factor derived from the raw RTT sample clamped between `REFERENCE_RTT_MS` (50ms) and `MAX_RTT_MS` (350ms): [1](#0-0) 

This value is fed by a single spot measurement taken once, at connection admission time, from the QUIC library's RTT estimator: [2](#0-1) 

`connection.rtt()` reflects handshake/path RTT as currently observed by `quinn`, which is influenced by the remote peer's own timing behavior (e.g., deliberately delaying its handshake responses/ACKs during the RTT sampling window that establishes the connection's initial RTT estimate). Because the resulting `max_uni_streams` is set once via `connection.set_max_concurrent_uni_streams(max_uni_streams)` at connection creation and is never re-derived from a smoothed/verified measurement afterward, a peer that inflates its perceived RTT at connection-establishment time can obtain up to `MAX_RTT_MS / REFERENCE_RTT_MS` (7x) more concurrent streams than its stake would otherwise entitle it to — directly analogous to reading a manipulable "spot" value once and using it, unverified, to determine a resource entitlement.

This is structurally the same bug class as the reported oracle issue: a critical allocation decision (position value / stream quota) is derived from a single spot reading of an externally-influenceable quantity (Uniswap pool price / connection RTT) instead of a time-weighted or otherwise robust measure.

### Impact Explanation
An attacker-controlled peer can obtain a disproportionately large concurrent-stream allocation relative to its actual stake by manipulating the RTT observed at connection setup, allowing it to open more QUIC streams (and thus push more transaction packets) than the stake-weighted QoS design intends. This is a QoS evasion of the very throttling mechanism (`SwQos`/stake-weighted stream limits) that Agave relies on to fairly allocate validator ingress bandwidth among unstaked/low-stake versus staked peers, letting a low-stake or unstaked-adjacent attacker consume disproportionate TPU ingress resources.

### Likelihood Explanation
Likelihood is medium: exploitation requires only the ability to influence the RTT observed by the server during the connection's initial RTT sample (achievable by a remote, unprivileged network peer through deliberate handshake/ACK delay), and this happens on the standard TPU/TPU-forward QUIC connection-establishment path (`spawn_stake_weighted_qos_server` → `SwQos::cache_new_connection`) used by any client connecting to a validator.

### Recommendation
Do not scale QoS stream limits from a single instantaneous RTT sample obtained at connection admission. Use a smoothed/validated RTT estimate (e.g., minimum RTT over multiple samples, or periodic re-measurement with outlier rejection) before applying BDP scaling, and bound the multiplier's influence on stake-derived limits so a manipulated RTT cannot materially exceed the peer's stake-appropriate stream quota.

### Proof of Concept
1. A remote peer establishes a QUIC connection to the validator's TPU/TPU-forward endpoint with low or zero stake.
2. During the QUIC handshake, the peer deliberately delays its responses (or routes through conditions that appear to increase path RTT) so that `connection.rtt()` reports a value near `MAX_RTT_MS` (350ms) rather than a normal LAN/WAN RTT.
3. `SwQos::cache_new_connection` reads this manipulated RTT and calls `compute_max_allowed_uni_streams_with_rtt`, which multiplies the peer's stake-derived stream allotment by up to `350/50 = 7x`.
4. `connection.set_max_concurrent_uni_streams(max_uni_streams)` locks in this inflated quota for the lifetime of the connection, allowing the attacker to open far more concurrent streams than its stake should permit, evading the intended stake-weighted QoS limits.

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
