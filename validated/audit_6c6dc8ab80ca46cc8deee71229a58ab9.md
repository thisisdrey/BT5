### Title
Unstaked (and staked) clients can inflate the observed connection RTT to make `compute_max_allowed_uni_streams_with_rtt` grant far more than `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` concurrent uni-streams, evading the intended per-tier QoS budget - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
`SwQos::cache_new_connection` derives the per-connection `max_concurrent_uni_streams` grant from `connection.rtt()`, an attacker-observable/attacker-influenceable QUIC RTT sample, and feeds it into `compute_max_allowed_uni_streams_with_rtt` without ever re-clamping the final scaled result to the tier's intended cap. An unstaked client that causes the server to measure a higher RTT (e.g. by delaying its handshake/ACK responses) receives a `max_concurrent_uni_streams` allocation up to 7x larger than the documented `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128) budget for its tier.

### Finding Description
In `streamer/src/nonblocking/swqos.rs`, `cache_new_connection` computes: [1](#0-0) 
which calls: [2](#0-1) 

For an unstaked peer, `streams` is fixed to `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128), but the final return value multiplies this by `rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS) / REFERENCE_RTT_MS`, where `REFERENCE_RTT_MS = 50` and `MAX_RTT_MS = 350`. This means the *actual* value passed to `connection.set_max_concurrent_uni_streams` ranges from 128 (at RTT ≤ 50ms) up to `128 * 350 / 50 = 896` (at RTT ≥ 350ms) — a 7x increase over the tier's documented maximum, and there is no final clamp back to `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`.

`connection.rtt()` reflects the QUIC connection's measured round-trip time, which is derived from the elapsed wall-clock time between a sent packet and its corresponding ACK. Because the remote peer (the attacker, as an unprivileged client) controls when it sends ACKs for packets/handshake steps addressed to it, it can deliberately delay those ACKs to inflate the RTT sample the server observes, without needing any staked/privileged access — this is a pure client-side timing choice, not something sigverify, connection-table pruning, or existing rate limiters in `stream_throttle.rs` (`throttle_stream`, `StakedStreamLoadEMA`) prevent, since those limiters bound the *rate* of new streams accepted, not the *concurrency* ceiling granted at the QUIC transport layer via `set_max_concurrent_uni_streams`.

The existing unit test in this same file explicitly demonstrates that scaling above the "max" constant is exactly what happens for `ConnectionPeerType::Unstaked`: [3](#0-2) 

### Impact Explanation
This falls under the QoS-evasion category explicitly permitted in scope: an unstaked, unprivileged remote client can obtain a `max_concurrent_uni_streams` grant on its QUIC connection far above the intended `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128) budget — up to 896 — simply by presenting a higher observed RTT to the server. Because each unstaked peer may open up to `DEFAULT_MAX_QUIC_CONNECTIONS_PER_UNSTAKED_PEER` (8) connections, and the unstaked connection table allows up to `DEFAULT_MAX_UNSTAKED_CONNECTIONS` (2000) total connections, an attacker repeating this across multiple connections can consume disproportionate per-connection stream/buffer resources (each concurrent stream reserves `stream_receive_window` capacity) relative to its intended unstaked-tier allocation, unfairly capturing more concurrent-stream budget than other unstaked senders and degrading available TPU capacity for legitimate unstaked traffic.

### Likelihood Explanation
No privileged/staked/operator access is required — only the ability to open a QUIC connection to the public TPU port and control the timing of the client's own protocol-level responses (handshake/ACKs), both of which are fully available to any unstaked remote client. The behavior is deterministic and reproducible: it depends only on `connection.rtt()` at the time `cache_new_connection` runs, which the existing unit test confirms scales the grant upward with RTT with no re-clamp to the tier ceiling.

### Recommendation
Clamp the final result of `compute_max_allowed_uni_streams_with_rtt` to the tier's intended maximum (`QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` for unstaked, `QUIC_MAX_STAKED_CONCURRENT_STREAMS` for staked) after BDP scaling, or bound the RTT sample used for scaling to a server-side trusted measurement (e.g., derived only from packets whose timing the server itself controls, or an average/median over multiple independent samples resistant to single delayed-ACK manipulation) rather than the raw `connection.rtt()` value.

### Proof of Concept
Add to `streamer/src/nonblocking/swqos.rs` test module:
```rust
#[test]
fn test_unstaked_streams_never_exceed_tier_cap_under_rtt_manipulation() {
    for rtt in [REFERENCE_RTT_MS, REFERENCE_RTT_MS + 1, MAX_RTT_MS / 2, MAX_RTT_MS, MAX_RTT_MS * 2] {
        let granted = compute_max_allowed_uni_streams_with_rtt(
            rtt,
            ConnectionPeerType::Unstaked,
            10_000,
        );
        assert!(
            granted <= QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
            "unstaked peer obtained {granted} streams (> cap {QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS}) at rtt={rtt}ms"
        );
    }
}
```
This test fails against current code (e.g., at `rtt = MAX_RTT_MS = 350`, `granted = 896 > 128`), confirming the invariant is violated and that an attacker-inflated RTT (achievable by delaying client-side ACKs/handshake responses) grants an out-of-tier `max_concurrent_uni_streams` allocation.

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

**File:** streamer/src/nonblocking/swqos.rs (L560-580)
```rust
    #[test]
    fn test_max_allowed_uni_streams_with_rtt() {
        assert_eq!(
            compute_max_allowed_uni_streams_with_rtt(
                REFERENCE_RTT_MS / 2,
                ConnectionPeerType::Unstaked,
                10000
            ),
            QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
            "Max streams should not be less than normal for low RTT"
        );
        assert_eq!(
            compute_max_allowed_uni_streams_with_rtt(
                REFERENCE_RTT_MS + REFERENCE_RTT_MS / 2,
                ConnectionPeerType::Unstaked,
                10000
            ),
            QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS + QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS / 2,
            "Max streams should scale with BDP in high-RTT connections"
        );
    }
```
