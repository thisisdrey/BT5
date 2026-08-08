### Title
Unstaked/low-stake QUIC peers can inflate `max_concurrent_uni_streams` up to ~7x the documented cap via RTT manipulation - ([File: streamer/src/nonblocking/swqos.rs])

### Summary
`compute_max_allowed_uni_streams_with_rtt` scales the per-connection uni-stream concurrency limit by the measured RTT for **all** peer types, including `ConnectionPeerType::Unstaked`, without ever re-clamping the scaled result back to `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` (128). Since `rtt_millis` comes directly from `connection.rtt()` (a QUIC/quinn RTT sample influenced by how quickly the remote peer acknowledges packets), a remote unstaked client can deliberately delay its ACKs to push the measured RTT toward `MAX_RTT_MS` (350ms) and receive a `max_uni_streams` value of up to `128 * 350 / 50 = 896` instead of the intended 128.

### Finding Description
In `streamer/src/nonblocking/swqos.rs`: [1](#0-0) 

For `ConnectionPeerType::Unstaked`, `streams` is set to the constant `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS = 128`, but the final line unconditionally applies BDP-style RTT scaling to **all** peer types (both staked and unstaked) with no post-scaling clamp back to the unstaked ceiling:
```
(streams.saturating_mul(rtt_millis.clamp(REFERENCE_RTT_MS, MAX_RTT_MS))) / REFERENCE_RTT_MS
```
The staked branch already clamps to `QUIC_MAX_STAKED_CONCURRENT_STREAMS` before this line, but that clamp happens before the RTT multiplication, not after — so both the staked and unstaked results are scaled beyond their documented ceilings.

This is invoked from `cache_new_connection`, which reads the connection RTT right at connection admission time and applies the resulting value via `connection.set_max_concurrent_uni_streams`: [2](#0-1) 

The existing unit test in the file itself demonstrates this behavior is present in the code (documented as "should scale with BDP"), showing an unstaked peer with `rtt_millis = 1.5 * REFERENCE_RTT_MS` gets `192` streams instead of the documented cap of `128`: [3](#0-2) 

An unprivileged remote client opening a QUIC connection to the TPU port controls when it acknowledges handshake/transport packets. By deliberately delaying its own ACKs (or exploiting an asymmetric/laggy path), it can inflate the `rtt_millis` sample used by `cache_new_connection` at the moment `try_add_connection`/`cache_new_connection` runs, before any throttling based on true throughput takes effect. There is no re-validation of `max_uni_streams` against `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS` after the RTT multiplication, so the resulting `VarInt` passed to `set_max_concurrent_uni_streams` can reach up to 896 for an unstaked peer (7x intended).

### Impact Explanation
This breaks the invariant that a connection's structural stream-concurrency ceiling is proportionate to stake and bounded at `QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS`/`QUIC_MAX_STAKED_CONCURRENT_STREAMS`. An unstaked or ultra-low-stake attacker can open up to ~7x more concurrent QUIC uni-streams per connection than intended, increasing the number of simultaneously open (unread/unverified) streams the server must buffer/track for that single connection. Although the separate `StakedStreamLoadEMA`/`throttle_stream` mechanism still rate-limits how fast new streams can be *accepted* into processing, the raw QUIC-level concurrency ceiling itself is a resource-allowance value that this scaling silently detaches from the stake-based design, allowing a resource-allowance escalation disproportionate to stake — a QoS/resource-allowance evasion issue (as opposed to a full sigverify/consensus bypass).

### Likelihood Explanation
Preconditions are minimal: any unstaked or ultra-low-stake remote client establishing a normal QUIC connection to the TPU port. Inflating the measured RTT only requires delaying local ACK transmission during/after the handshake, which is entirely under the attacker's control and does not require any privileged access, staking, or protocol violation — it's within the standard latitude of a QUIC endpoint. This is trivially repeatable per-connection.

### Recommendation
Clamp the RTT-scaled result to the appropriate per-peer-type maximum after the multiplication, e.g.:
```rust
match peer_type {
    ConnectionPeerType::Unstaked => scaled.min(QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS),
    ConnectionPeerType::Staked(_) => scaled.min(QUIC_MAX_STAKED_CONCURRENT_STREAMS),
}
```
so BDP scaling can only be used to *reach* the existing documented ceiling faster on high-latency links, never exceed it.

### Proof of Concept
```rust
// streamer/src/nonblocking/swqos.rs (add to test mod)
#[test]
fn test_unstaked_streams_never_exceed_documented_cap() {
    for rtt in [REFERENCE_RTT_MS, REFERENCE_RTT_MS + 1, MAX_RTT_MS / 2, MAX_RTT_MS, MAX_RTT_MS * 2] {
        let streams = compute_max_allowed_uni_streams_with_rtt(
            rtt,
            ConnectionPeerType::Unstaked,
            10_000,
        );
        assert!(
            streams <= QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS,
            "unstaked peer obtained {streams} streams at rtt={rtt}ms, exceeding documented cap {QUIC_MAX_UNSTAKED_CONCURRENT_STREAMS}"
        );
    }
}
```
Running this test against the current implementation fails once `rtt > REFERENCE_RTT_MS` (e.g., at `MAX_RTT_MS = 350`, `streams = 896`), confirming the cap is not enforced and is attacker-influenceable via RTT.

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
