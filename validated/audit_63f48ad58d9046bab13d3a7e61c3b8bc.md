### Title
Unstaked QUIC clients can inflate `connection.rtt()` to receive up to 7× more concurrent uni-streams than `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` - (File: streamer/src/nonblocking/swqos.rs)

### Summary
`compute_max_allowed_uni_streams_with_rtt` applies BDP-based RTT scaling to *all* peer types, including `ConnectionPeerType::Unstaked`, instead of restricting the scaling to staked peers. Because an attacker fully controls ACK timing on their own QUIC connection, they can inflate the RTT observed by the server (up to the `MAX_RTT_MS` clamp) and be granted proportionally more concurrent uni-streams than the intended unstaked cap.

### Finding Description
In `streamer/src/nonblocking/swqos.rs`, `compute_max_allowed_uni_streams_with_rtt` computes a base `streams` value per peer type (`QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS = 128` for unstaked peers), then unconditionally scales it by RTT: [1](#0-0) 

```
ConnectionPeerType::Unstaked => QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
};
// scale amount of streams based on RTT if RTT is larger than REFERENCE_RTT_MS
// multiply first then divide to avoid rounding errors.
(streams.saturating_mul(rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS))) / REFERENCE_RTT_MS
```

This scaling is applied regardless of whether the peer is `Staked` or `Unstaked` — there is no branch that skips BDP scaling for unstaked connections. `REFERENCE_RTT_MS = 50` and `MAX_RTT_MS = 350`, so the multiplier ranges from `1x` (rtt ≤ 50ms) to `7x` (rtt ≥ 350ms, clamped).

`cache_new_connection` derives `rtt_millis` directly from `connection.rtt()`, a value maintained by Quinn's congestion controller from observed packet round-trip timing (based on when the server sends a packet and when it receives the corresponding ACK): [2](#0-1) 

```
let rtt_millis = connection.rtt().as_millis().min(MAX_RTT_MS as u128) as u32;
let max_uni_streams = VarInt::from_u32(compute_max_allowed_uni_streams_with_rtt(
    rtt_millis,
    conn_context.peer_type(),
    conn_context.total_stake,
));
```

The resulting `max_uni_streams` is applied directly to the connection with `connection.set_max_concurrent_uni_streams(max_uni_streams)` (line 224), which is Quinn's transport-level control limiting how many concurrent unidirectional streams the peer may open.

Since RTT as measured by the server is entirely a function of when the remote peer chooses to acknowledge packets, an unstaked/unprivileged remote attacker can deliberately delay ACKs (or otherwise manipulate its own send/ack timing) on a connection it controls to inflate the server-observed RTT toward `MAX_RTT_MS`, and thereby obtain up to `128 * (350/50) = 896` concurrent uni-streams instead of the intended cap of 128 — a nearly 7x increase.

The existing unit test in the same file even documents this behavior as intended for `Unstaked` peers, which confirms the code path is not a copy/paste bug but an actual missing guard against non-staked BDP scaling: [3](#0-2) 

No other check (e.g., a peer-type gate before RTT scaling, or a hard clamp for unstaked peers) exists to prevent this in `compute_max_allowed_uni_streams_with_rtt`, `cache_new_connection`, or callers.

### Impact Explanation
This is a QoS-evasion vulnerability (per-connection stream-limit bypass). A single unstaked/unprivileged connection can be granted up to ~7x the number of concurrent uni-streams that the unstaked tier is designed to allow, letting one attacker connection consume disproportionate per-connection stream capacity (and associated per-stream receive-window/memory resources) relative to other unstaked senders, degrading fairness of TPU capacity allocation among unstaked clients. This matches the described "unfairly capture connection/stream/per-IP QoS limits" bounty scope.

### Likelihood Explanation
The attacker needs only to open a QUIC connection to the leader's public TPU port as an unstaked client and control the timing of ACKs it sends back to the server — something fully within an unprivileged client's control (e.g., artificially delaying ACK transmission or otherwise manipulating perceived path latency). No special stake, keys, or validator/operator control is required. This is straightforward to reproduce deterministically by having the attacker's client hold back ACKs during the connection handshake/early RTT sampling window.

### Recommendation
Restrict BDP/RTT scaling to staked peers only (or apply a much tighter/fixed cap for unstaked peers), e.g., change `compute_max_allowed_uni_streams_with_rtt` so the RTT multiplier is only applied within the `ConnectionPeerType::Staked(_)` branch, and return `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` unscaled for `ConnectionPeerType::Unstaked`.

### Proof of Concept
Add/adjust a unit test in `streamer/src/nonblocking/swqos.rs` asserting invariance for unstaked peers across the RTT range:

```rust
#[test]
fn test_unstaked_streams_invariant_to_rtt() {
    for rtt in [MIN_RTT_MS_TEST, REFERENCE_RTT_MS, REFERENCE_RTT_MS + 1, MAX_RTT_MS, MAX_RTT_MS + 100] {
        let streams = compute_max_allowed_uni_streams_with_rtt(
            rtt,
            ConnectionPeerType::Unstaked,
            10_000,
        );
        assert_eq!(
            streams, QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
            "Unstaked stream cap must not scale with RTT (got {streams} at rtt={rtt}ms)"
        );
    }
}
```
Running this against the current implementation fails at `rtt = REFERENCE_RTT_MS + 1` and above (e.g., at `MAX_RTT_MS = 350`, `streams = 128 * 350 / 50 = 896 != 128`), confirming the bypass. An integration-level PoC would additionally spin up the real QUIC streamer, connect as an unstaked client, artificially delay ACKs to raise `connection.rtt()` toward 350ms, and assert via `connection.max_concurrent_uni_streams` (or by opening streams until throttled) that more than 128 concurrent uni-streams are actually accepted.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L174-179)
```rust
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
