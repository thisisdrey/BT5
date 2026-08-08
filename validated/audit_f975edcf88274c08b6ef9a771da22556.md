### Title
Stake-weighted QoS uses attacker-controlled QUIC RTT to inflate max concurrent streams, bypassing intended per-peer stream limits - (File: streamer/src/nonblocking/swqos.rs)

### Summary
The stake-weighted QoS controller (`SwQos`) computes the maximum number of concurrent unidirectional QUIC streams a peer is allowed to open by scaling the stake/peer-type-derived base value with the connection's measured round-trip time (RTT). Because RTT, as measured by `quinn::Connection::rtt()`, is an externally observable value entirely controlled by the timing of the remote peer's own ACKs, an unprivileged client can artificially inflate its perceived RTT to obtain up to ~7x more concurrent streams than the protocol's stake-weighted (or unstaked) design intends — without acquiring any additional stake. This mirrors the reported bug class: a value meant to reflect a legitimate, checked quantity (deposited ETH / genuine network latency) is instead directly manipulable by an attacker and used unchecked in a security-relevant calculation (max price / max concurrent streams), bypassing the intended proportional allocation.

### Finding Description
`compute_max_allowed_uni_streams_with_rtt` first derives a base stream count from `peer_type`/stake ratio, then scales it by RTT: [1](#0-0) 

The RTT value used is taken directly from the QUIC connection object and only clamped to an upper bound, not validated against any independent source: [2](#0-1) 

`connection.rtt()` (quinn) reflects the smoothed round-trip-time computed from the timing of the client's own ACKs. A client fully controls when it acknowledges packets, so it can trivially make the server observe an inflated RTT (e.g., delaying ACKs, or exploiting normal network conditions) up to `MAX_RTT_MS` (350ms) versus the `REFERENCE_RTT_MS` (50ms) baseline: [3](#0-2) 

This yields a `streams * (rtt/50)` multiplier, i.e. up to 7x the intended per-stake or per-unstaked-peer stream ceiling — for example an unstaked peer's cap of 128 can be inflated to ~896, and a staked peer's per-connection cap of up to 512 can be inflated to ~3584. This limit is set once via `connection.set_max_concurrent_uni_streams(max_uni_streams)` at connection setup: [4](#0-3) 

Note this differs from the separate rate-limiting mechanism (`throttle_stream` / `StakedStreamLoadEMA`), which is stake-based and not RTT-dependent, so the actual sustained packet-processing rate is unaffected; what the attacker gains is a larger allowed concurrency ceiling (more simultaneously open streams / buffers) than the protocol's stake-weighted design intends, entirely by self-reporting/inducing a fake network condition rather than by acquiring stake.

### Impact Explanation
An unprivileged remote peer (staked or unstaked, reachable via the TPU/TPU-forward QUIC endpoints) can obtain a materially higher concurrent-stream allowance than its stake or peer-type entitles it to, by simply manipulating observed RTT. This is a QoS evasion of the stake-weighted fairness/anti-DoS design: an attacker with low or zero stake can hold open substantially more concurrent unidirectional streams than intended, increasing per-connection resource consumption (stream/flow-control state) on the validator relative to what the design assumes for that stake class. This directly matches the "QoS evasion" impact category.

### Likelihood Explanation
High: RTT manipulation requires no special access, keys, or stake — any client connecting over QUIC can influence measured RTT purely by controlling ACK timing, which is a normal client-side capability, not an implementation bug elsewhere that needs to be chained. The affected code path (`cache_new_connection`) executes on every new connection admitted by `SwQos`, which is the default stake-weighted QoS controller used by `spawn_stake_weighted_qos_server` for TPU/TPU-forward.

### Recommendation
Do not scale the security-relevant `max_concurrent_uni_streams` limit using a value the remote peer can directly influence (RTT). If BDP-based scaling is desired, base it on a value not trivially forgeable by the client (e.g., server-side measured/estimated path RTT sampled independently of ACK timing, or a fixed conservative scaling factor), or cap the scaling factor much more conservatively and re-validate/adjust it periodically rather than trusting a single RTT sample taken at connection-establishment time.

### Proof of Concept
1. Establish a QUIC connection to a validator's TPU port as either an unstaked client or a low-stake identity.
2. Before/while the server calls `cache_new_connection` (on connection admission), delay client-side ACKs to inflate the smoothed RTT observed by `quinn` up to at least 350ms (e.g., via TC netem-style artificial delay, or a slow/idle-ACK client implementation).
3. Observe that `compute_max_allowed_uni_streams_with_rtt` computes a `max_uni_streams` value up to 7x larger than the value that would be computed at `REFERENCE_RTT_MS` (50ms) for the same stake/peer type, as verified directly by the existing unit test that documents this scaling behavior: [5](#0-4) 
4. Confirm via server logs (`debug!("Peer type {:?}, total stake {}, max streams {} ...")`, line 224-231) that the client's connection is granted a `max_concurrent_uni_streams` far above what its stake/peer-type would ordinarily allow at nominal RTT.

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L50-54)
```rust
/// RTT after which we start BDP scaling
const REFERENCE_RTT_MS: u32 = 50;

/// Above this RTT we stop scaling for BDP
const MAX_RTT_MS: u32 = 350;
```

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

**File:** streamer/src/nonblocking/swqos.rs (L224-224)
```rust
            connection.set_max_concurrent_uni_streams(max_uni_streams);
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
