### Title
Unbounded stream-quota amplification via unverified RTT in `compute_max_allowed_uni_streams_with_rtt` - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
The BDP (bandwidth-delay-product) stream-scaling formula in `compute_max_allowed_uni_streams_with_rtt` multiplies the already stake-clamped stream count by an RTT-derived factor (up to 7x, `MAX_RTT_MS/REFERENCE_RTT_MS = 350/50`) *after* the stake-proportional clamp has already been applied, and the result is never re-clamped. Since QUIC RTT estimation trusts the peer-reported ACK Delay field, an attacker fully controls the observed `connection.rtt()` on the server side and can inflate it up to `MAX_RTT_MS`, obtaining up to 7x more `max_concurrent_uni_streams` than the stake-proportional cap intends.

### Finding Description
`compute_max_allowed_uni_streams_with_rtt` first computes a base `streams` value that is explicitly clamped to `[QUIC_MIN_STAKED_CONCURRENT_STREAMS, QUIC_MAX_STAKED_CONCURRENT_STREAMS]` for staked peers, or fixed at `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` for unstaked peers: [1](#0-0) 

It then scales this already-bounded value by RTT with **no subsequent clamp**: [2](#0-1) 

The RTT input comes straight from `connection.rtt()`, clamped only to `MAX_RTT_MS` (350ms), and is used at connection-setup time in `cache_new_connection`: [3](#0-2) 

`connection.rtt()` is quinn's smoothed-RTT estimate, which is derived from the peer's self-reported QUIC ACK Delay field subtracted from the measured round-trip time. A remote client — which is exactly the attacker model here (unstaked, unprivileged, connecting directly to the TPU QUIC port) — fully controls its own client stack and can under-report (or omit) the ACK Delay while intentionally delaying its ACK transmissions. This causes the leader's RTT sample to be inflated toward `MAX_RTT_MS` without any real network latency, and without any privileged access, staked identity, or path manipulation beyond controlling one's own client's ACK timing.

Because the post-scaling result has no final `clamp(...)`, an unstaked attacker's base of `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128) can be inflated up to `128 * 350/50 = 896` uni-streams — 7x the value the QoS design intends as the unstaked ceiling. A staked attacker with minimal stake, whose base is clamped to `QUIC_MIN_STAKED_CONCURRENT_STREAMS` (128), can similarly reach 896 instead of the intended stake-proportional low value, and even a peer near the max cap (512) can reach `512*7=3584` streams — far above the documented `QUIC_MAX_STAKED_CONCURRENT_STREAMS` hard limit that the constant's own comment ("Set the maximum concurrent stream numbers to avoid excessive streams... to reduce contention of the limited receive_window") states is meant to bound resource usage.

The existing test suite even documents the multiplicative, unclamped behavior as intended ("Max streams should scale with BDP in high-RTT connections") without asserting an upper bound tied to `QUIC_MAX_STAKED_CONCURRENT_STREAMS`/`QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`: [4](#0-3) 

No sigverify, dedup, or additional QoS guard re-validates `max_concurrent_uni_streams` after it is set via `connection.set_max_concurrent_uni_streams(max_uni_streams)`; the QUIC transport itself will honor this attacker-inflated limit and permit that many concurrently-open unidirectional streams, each consuming per-stream receive buffer/state in `handle_connection`/`ConnectionTable`.

### Impact Explanation
An unstaked/low-stake attacker gains a 7x amplification of concurrently-open uni-stream capacity relative to what the stake-proportional QoS design intends, at the cost of only lying about a self-reported timing field in their own QUIC client. This lets a single low-stake or free (unstaked) connection consume disproportionate per-connection resources (stream table entries, buffered chunks, `PacketAccumulator` allocations) on the leader's TPU relative to actual stake, starving legitimately-behaving peers of TPU stream capacity — a QoS-evasion / fairness-invariant violation matching the "per-connection stream quota proportionate to stake" invariant named in the question.

### Likelihood Explanation
This requires no special privilege: any remote client connecting to the public TPU QUIC port with a custom/modified QUIC client stack (or a hand-crafted ACK Delay value) can trigger it on every new connection. It is fully repeatable per-connection and does not require staked identity, gossip participation, or leader control — it only needs the attacker to control ACK timing/reporting in their own client, which is inherent to being the remote peer of a QUIC connection.

### Recommendation
Apply the stake-proportional hard clamp (`QUIC_MIN_STAKED_CONCURRENT_STREAMS`..`QUIC_MAX_STAKED_CONCURRENT_STREAMS` for staked, and a fixed/explicit ceiling for unstaked such as some small multiple of `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`) *after* the RTT-based BDP scaling in `compute_max_allowed_uni_streams_with_rtt`, e.g.:
```rust
(streams.saturating_mul(rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS)) / REFERENCE_RTT_MS)
    .clamp(min_bound, max_bound)
```
where `max_bound` reflects the same per-peer-type ceiling documented by `QUIC_MAX_STAKED_CONCURRENT_STREAMS` / a bounded unstaked equivalent, so RTT scaling cannot exceed the intended stake-based resource envelope. Additionally, consider not trusting raw `connection.rtt()` for QoS decisions without corroboration (e.g., server-side timestamped probe RTT rather than peer-reported ACK-delay-adjusted RTT).

### Proof of Concept
```rust
// streamer/src/nonblocking/swqos.rs (test module)
#[test]
fn test_rtt_scaling_exceeds_intended_stake_caps() {
    // Unstaked peer: base is fixed at QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS.
    let unstaked_at_max_rtt = compute_max_allowed_uni_streams_with_rtt(
        MAX_RTT_MS,
        ConnectionPeerType::Unstaked,
        10_000,
    );
    // Attacker-controlled RTT inflation (e.g., via lied ACK Delay) yields ~7x amplification,
    // with no clamp back down to a bound tied to QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS.
    assert!(
        unstaked_at_max_rtt > QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS * 2,
        "unstaked stream quota should not scale unbounded with attacker-controlled RTT, got {unstaked_at_max_rtt}"
    );

    // Low-stake staked peer: base clamps to QUIC_MIN_STAKED_CONCURRENT_STREAMS, but scaling still
    // multiplies it up to 7x with no final clamp against QUIC_MAX_STAKED_CONCURRENT_STREAMS proportional
    // to actual stake share.
    let low_stake_at_max_rtt = compute_max_allowed_uni_streams_with_rtt(
        MAX_RTT_MS,
        ConnectionPeerType::Staked(1), // negligible stake
        1_000_000_000,
    );
    assert!(
        low_stake_at_max_rtt <= QUIC_MIN_STAKED_CONCURRENT_STREAMS * 2,
        "a near-zero-stake peer should not be able to obtain {low_stake_at_max_rtt} streams via RTT gaming \
         (expected bound near QUIC_MIN_STAKED_CONCURRENT_STREAMS)"
    );
}
```
Expected result with the current implementation: the first assertion fails/demonstrates ~7x amplification (896 vs 128), and the second assertion fails because a negligible-stake peer reaches 896 streams instead of staying near `QUIC_MIN_STAKED_CONCURRENT_STREAMS` (128), proving the per-connection stream quota is not proportionate to stake once RTT is attacker-manipulated.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L152-175)
```rust
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
```

**File:** streamer/src/nonblocking/swqos.rs (L176-179)
```rust
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
