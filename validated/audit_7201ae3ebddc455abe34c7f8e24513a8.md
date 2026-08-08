### Title
Unstaked peers can inflate observed QUIC RTT to obtain up to 7x more concurrent uni-streams than `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` - (File: streamer/src/nonblocking/swqos.rs)

### Summary
`compute_max_allowed_uni_streams_with_rtt` scales the per-connection `max_concurrent_uni_streams` limit up when the observed `connection.rtt()` exceeds `REFERENCE_RTT_MS`, with no distinction for `ConnectionPeerType::Unstaked`. Because a QUIC endpoint can inflate its own perceived RTT by delaying acknowledgments, an unprivileged, unstaked client can cause the leader to grant it far more concurrent streams than the documented/intended `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` cap.

### Finding Description
`compute_max_allowed_uni_streams_with_rtt` sets `streams = QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128) for `ConnectionPeerType::Unstaked`, then unconditionally applies BDP scaling: [1](#0-0) 
`(streams * rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS)) / REFERENCE_RTT_MS`, where `REFERENCE_RTT_MS = 50` and `MAX_RTT_MS = 350`. This is called from `cache_new_connection` using the live, client-influenceable `connection.rtt()` value: [2](#0-1) 

Since `connection.rtt()` is an application-visible smoothed RTT sample derived from how quickly the peer acknowledges packets, and the peer fully controls the timing of its own ACKs (subject only to the negotiated `max_ack_delay`), an unstaked client can deliberately delay its acknowledgments to make the server observe an RTT approaching `MAX_RTT_MS` even over a low-latency path. This is not blocked by any check in the reachable code path — the peer-type check only exists to pick the *base* stream count (128 for unstaked, stake-proportional for staked); the RTT-scaling multiplier is applied identically regardless of peer type.

The repository's own test suite already demonstrates that unstaked peers are *not* bounded by `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` once RTT exceeds the reference value: [3](#0-2) 
At `rtt_millis = MAX_RTT_MS` (350ms, attacker-achievable), the allocation becomes `128 * 350 / 50 = 896` — 7x the documented "empirically found max ... that seems to maximize TPS" limit for unstaked peers referenced in the comment at line 36-39.

None of the existing guards (per-IP connection limits, `max_connections_per_unstaked_peer`, `prune_unstaked_connection_table`, or the stream-rate throttling in `throttle_stream`/`StakedStreamLoadEMA`) constrain the *concurrent* uni-stream count set via `connection.set_max_concurrent_uni_streams(max_uni_streams)`; that value is set once at connection admission based on the momentary RTT sample and governs how many streams (and associated per-stream QUIC receive buffers) the peer may have open simultaneously for the lifetime of the connection.

### Impact Explanation
An unstaked, unprivileged remote client can obtain up to ~7x the intended maximum concurrent uni-streams per connection by simply delaying its own ACKs to inflate observed RTT toward `MAX_RTT_MS`. This lets a single unstaked connection hold open substantially more simultaneous streams than `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` was designed to permit, increasing per-connection QUIC receive-buffer memory, and downstream buffering/dedup/sigverify queuing pressure disproportionate to the unstaked tier's intended allotment. This falls under QoS/stream-limit evasion for unstaked peers.

### Likelihood Explanation
Highly feasible and fully attacker-controlled: no stake, no validator/gossip access, and no special privileges are required — only opening a QUIC connection to the public TPU port and controlling ACK timing (a standard client-side capability), which is trivial to reproduce deterministically and repeatably across connections.

### Recommendation
Do not apply RTT-based BDP scaling for `ConnectionPeerType::Unstaked` (or clamp the unstaked result to `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` regardless of RTT), since unstaked peers have no economic stake backing the additional resource consumption. If BDP scaling for unstaked peers is genuinely desired, base the RTT measurement on a value the peer cannot manipulate (e.g., handshake-derived path RTT rather than the live smoothed RTT), or cap the scaling factor much more conservatively for the unstaked tier.

### Proof of Concept
```rust
// streamer/src/nonblocking/swqos.rs (test module)
#[test]
fn test_unstaked_streams_bounded_regardless_of_rtt() {
    for rtt in REFERENCE_RTT_MS..=MAX_RTT_MS {
        let streams = compute_max_allowed_uni_streams_with_rtt(
            rtt,
            ConnectionPeerType::Unstaked,
            10_000,
        );
        assert!(
            streams <= QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
            "unstaked stream cap {streams} exceeded QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS \
             ({QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS}) at rtt={rtt}ms"
        );
    }
}
```
Running this against current `compute_max_allowed_uni_streams_with_rtt` fails at `rtt = 51ms` and above, reaching 896 at `rtt = 350ms`, confirming the unstaked cap is not enforced across the RTT range as intended.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L174-178)
```rust
        ConnectionPeerType::Unstaked => QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
    };
    // scale amount of streams based on RTT if RTT is larger than REFERENCE_RTT_MS
    // multiply first then divide to avoid rounding errors.
    (streams.saturating_mul(rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS))) / REFERENCE_RTT_MS
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

**File:** streamer/src/nonblocking/swqos.rs (L571-580)
```rust
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
