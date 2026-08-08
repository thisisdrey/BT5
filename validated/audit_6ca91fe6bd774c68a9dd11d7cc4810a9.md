Based on my analysis of `last_update`, it is set once at connection creation (`Arc::new(AtomicU64::new(timing::timestamp()))` in `SwQos::build_connection_context`, `streamer/src/nonblocking/swqos.rs:312,337`) and I could not confirm any code path that refreshes it later on stream activity within the excerpts read — `prune_oldest`/`prune_random` operate purely on this creation-time timestamp/stake, and are invoked *only* from inside `SwQos::try_add_connection`/`prune_unstaked_connections_and_add_new_connection`, i.e., reactively, only when a *new* connection attempt reaches that code path.

### Title
Unprivileged attacker can permanently starve legitimate connections by parking idle post-handshake QUIC connections against the global `max_concurrent_connections` gate - (File: streamer/src/nonblocking/quic.rs)

### Summary
`ClientConnectionTracker::new` enforces a single global atomic ceiling (`stats.open_connections < max_concurrent_connections`, 125% of `max_staked_connections + max_unstaked_connections`) *before* any peer-type/stake classification or per-table quota/pruning logic runs. An attacker who establishes many connections, completes the handshake, and then never opens a stream keeps each `ClientConnectionTracker` alive (its `Drop` is the only thing that decrements the counter), permanently occupying slots. Because the type-aware eviction logic (`prune_random`/`prune_oldest`) only runs inside `QosController::try_add_connection`, and that code is only reached *after* passing the earlier global gate, legitimate connections that get refused at the gate never get a chance to trigger eviction of the attacker's stale connections.

### Finding Description
The admission flow in `run_server` (`streamer/src/nonblocking/quic.rs:331-379`) is:
1. Rate-limit checks (overall + per-IP).
2. `ClientConnectionTracker::new(stats.clone(), qos.max_concurrent_connections())` (`quic.rs:236-252`) — this only compares against a single global `AtomicUsize` (`stats.open_connections`), with **no distinction between idle and active connections, and no per-type (staked/unstaked) accounting** at this stage. If the atomic is already at/above the limit, `incoming.refuse()` is called and `refused_connections_too_many_open_connections` is incremented (`quic.rs:371-379`).
3. Only *after* this succeeds does the connection proceed to handshake (`setup_connection`) and then `qos.try_add_connection` (e.g. `SwQos::try_add_connection`, `swqos.rs:344-410`), which is where the *only* eviction/pruning logic lives (`prune_random` for staked, `prune_unstaked_connection_table`/`prune_oldest` for unstaked), gated on `connection_table_l.total_size >= max_*_connections`.

The `ClientConnectionTracker` is only dropped (decrementing `stats.open_connections`) when the connection is removed from a `ConnectionTable` (via `remove_connection`, `prune_oldest`, `prune_random`) or rejected during setup. Once a connection is admitted into a per-type table, nothing removes it purely due to being idle/stream-less — the QUIC transport `max_idle_timeout` is set to 30s (`QUIC_MAX_TIMEOUT`, `streamer/src/quic.rs:38,119-120`), but QUIC idle timeout resets on receipt of *any* packet, not specifically application stream data, so a client-controlled QUIC keep-alive/PING defeats it without ever opening a `solana-tpu` uni-stream. There is no server-side "must open first stream within N seconds" timeout — `wait_for_chunk_timeout` (`quic.rs:526,651-676`) only bounds reads *after* a stream has already been opened; the outer loop's `connection.accept_uni()` (`quic.rs:613-622`) waits indefinitely for a first stream with no deadline.

The reactive pruning in the per-type `ConnectionTable` is keyed off `last_update`, set once at `build_connection_context` time (`swqos.rs:312,337`) with no observed refresh on later activity in the reviewed code, so idle attacker connections are the most prunable — but that pruning is *never invoked* unless a *new* connection successfully reaches `qos.try_add_connection`. If the attacker has already saturated the global `stats.open_connections` counter up to `max_concurrent_connections` (125% ceiling), legitimate connections are refused at the earliest, type-agnostic gate in `run_server` and never reach `try_add_connection`, so the self-healing pruning logic that would otherwise evict the attacker's stale connections is never triggered. This produces a starvation deadlock: the mechanism designed to bound resource usage (the global gate) and the mechanism designed to evict abusive/stale occupants (per-table pruning) are sequenced such that the former can permanently block the latter from ever running.

### Impact Explanation
Legitimate unstaked *and staked* clients are refused new TPU QUIC connections (`incoming.refuse()`, `refused_connections_too_many_open_connections` stat, `quic.rs:371-379`) even though the attacker sends zero transactions and consumes no sigverify/scheduling/PoH resources. This is a connection-admission denial-of-service against the leader's TPU ingress path, falling under a QoS-evasion / ingress-availability bounty category: an unstaked, unprivileged attacker can bypass the intended fairness/pruning design and deny connection slots to legitimate stakers/clients.

### Likelihood Explanation
Preconditions: attacker needs enough concurrent connection slots to reach `max_concurrent_connections` (default 125% × (2000+2000) = 5000). Since a single source IP is bounded by `max_connections_per_unstaked_peer` (default 8) and the per-IP rate limiter (`max_connections_per_ipaddr_per_min`, default 8/min, burst 80), the attacker needs on the order of hundreds of distinct source IPs (~5000/8 ≈ 625) to hold enough concurrent idle connections — feasible for a modestly resourced botnet/VPS-with-many-IPs attacker, and nothing in the described attacker model (unstaked remote client(s) opening QUIC connections) excludes multiple source addresses. Once slots are parked, the attack is passive (just periodic QUIC-level keep-alives) and persists indefinitely since no idle-without-stream timeout or periodic sweep exists to reclaim them absent a new incoming connection attempt reaching `try_add_connection`.

### Recommendation
- Add a per-connection "must open first stream within T seconds" deadline enforced server-side in `handle_connection`/`setup_connection` (independent of the QUIC transport idle timeout, which is client-influenced), closing/evicting connections that never produce a stream.
- Decouple the global `max_concurrent_connections` gate from strict FIFO/atomic admission: when the global counter is at capacity, invoke the same type-aware pruning (`prune_oldest`/`prune_random`, keyed by real activity/last-stream-time rather than only connection-creation time) *before* refusing, so stale/idle occupants are evicted to make room for new admission attempts, mirroring the existing per-table reactive pruning.
- Update `last_update` on stream open/close (not just at connection creation) so "activity" is what protects a slot from pruning, not merely completed handshake.

### Proof of Concept
Integration test (extend existing harness in `streamer/src/nonblocking/quic.rs` tests / `setup_quic_server`):
```rust
#[tokio::test(flavor = "multi_thread")]
async fn test_idle_connections_starve_legitimate_clients() {
    let SpawnTestServerResult { join_handle, server_address, stats, cancel, .. } =
        setup_quic_server(
            None,
            QuicStreamerConfig::default_for_tests(),
            SwQosConfig {
                max_staked_connections: 4,
                max_unstaked_connections: 4,
                max_connections_per_unstaked_peer: 1, // 1 per source IP for test simplicity
                ..Default::default()
            },
        );

    // Simulate N distinct-IP attacker connections reaching 125% * (4+4) = 10 slots.
    // (In a real integration test this requires binding multiple client sockets/addresses.)
    let mut attacker_conns = Vec::new();
    for _ in 0..10 {
        let conn = make_client_endpoint(&server_address, None).await;
        attacker_conns.push(conn); // never open a stream
    }

    // Legitimate client attempts a connection.
    let legit = make_client_endpoint(&server_address, None).await;
    // Expect refusal via the global gate, not per-type pruning.
    assert_eq!(
        stats.refused_connections_too_many_open_connections.load(Ordering::Relaxed),
        1,
        "legitimate client was refused despite attacker sending zero transactions"
    );

    cancel.cancel();
    join_handle.await.unwrap();
}
```
Expected assertion under the current implementation: `refused_connections_too_many_open_connections` increments for the legitimate client even though none of the attacker connections sent any stream/transaction data, demonstrating the starvation described above. (Note: a fully faithful reproduction requires distinct source IPs per attacker connection to satisfy `max_connections_per_unstaked_peer`/per-IP rate limiting, which is a test-harness/networking detail rather than a logic gap in the vulnerable code.)