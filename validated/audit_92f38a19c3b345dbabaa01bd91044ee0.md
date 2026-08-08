### Title
Unstaked attacker can exhaust the global pre-handshake connection-tracker budget on the TPU QUIC port - ([File: streamer/src/nonblocking/quic.rs, streamer/src/nonblocking/qos.rs, streamer/src/nonblocking/simple_qos.rs])

### Summary
`ClientConnectionTracker::new` and the shared `open_connections` counter are incremented for every incoming QUIC connection, staked or unstaked, before the peer's stake/pubkey is known and before `QosController::try_add_connection` resolves. Since `max_concurrent_connections()` is a single shared budget (`max_staked_connections * 5/4` in `SimpleQos`) gating admission irrespective of peer type, an unstaked/anonymous attacker who stalls the handshake can occupy that entire budget for up to the handshake timeout, causing all other incoming connections (including staked ones) to be refused.

### Finding Description
`QosController::max_concurrent_connections` in `streamer/src/nonblocking/qos.rs` is documented and implemented (in `SimpleQos::max_concurrent_connections`, `streamer/src/nonblocking/simple_qos.rs` lines 422-425) as "Allow 25% more connections than required to allow for handshake" — i.e. it is a fixed slot pool sized off `max_staked_connections`, deliberately over-provisioned solely to tolerate in-flight handshakes. [1](#0-0) 

Crucially, `SimpleQosConnectionContext`/`try_add_connection` only admits `ConnectionPeerType::Staked(_)` peers into the actual connection table — `ConnectionPeerType::Unstaked => None` is returned unconditionally, meaning unstaked connections are never tracked or limited by the staked connection table itself: [2](#0-1) 

However, the peer's stake/pubkey can only be determined *after* the TLS/QUIC handshake progresses far enough to read the client certificate (`get_connection_stake`), so the shared, stake-agnostic admission gate — the `ClientConnectionTracker` created in `streamer/src/nonblocking/quic.rs` and counted via `stats.open_connections` against `qos.max_concurrent_connections()` — is consulted for every connection attempt regardless of whether it will ultimately be staked or unstaked. An attacker who opens connections and deliberately stalls completion of the handshake (e.g., sends the Initial packet but withholds/delays Handshake completion) keeps its `ClientConnectionTracker` (and thus its slot in `open_connections`) alive for close to the full `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` (2s) without ever needing a staked identity, since the tracker/slot accounting happens before `try_add_connection`'s staked/unstaked branch is even reached.

By repeating this with connection churn, the attacker can keep `open_connections` pinned at `qos.max_concurrent_connections()` continuously, causing the server's admission logic to refuse all further incoming connections via the `refused_connections_too_many_open_connections` counter path in `run_server`/`ClientConnectionTracker::new` — starving legitimate staked and unstaked clients alike of the fixed concurrent-connection budget, even though the budget was explicitly sized as a margin "to allow for handshake" and not intended to be consumable indefinitely by unauthenticated peers.

### Impact Explanation
Scoped impact: the leader's TPU QUIC port refuses all new connections (staked and unstaked) for the duration of the attack, since the shared `max_concurrent_connections()` slot pool — intended only as handshake headroom on top of `max_staked_connections` — can be fully occupied by anonymous, unauthenticated unstaked connections that never need to complete a real handshake. This matches a network-level denial-of-service against transaction ingestion (TPU) rather than a consensus/state-correctness bug.

### Likelihood Explanation
Preconditions are minimal: any unstaked remote client can open raw UDP/QUIC connections to the public TPU port with no identity verification prior to handshake. The attack requires no stake, no special config, and is fully repeatable/continuous by cycling connections that stall mid-handshake, keeping the shared counter saturated indefinitely.

### Recommendation
Decouple the pre-identity ("half-open"/in-handshake) connection admission limit from the staked-connection budget, e.g.:
- Track and cap "connections currently in handshake" separately from `max_concurrent_connections`, with a strict, small quota reserved specifically for unauthenticated/unstaked handshakes (distinct from the staked headroom).
- Enforce per-IP limits on concurrent in-handshake connections so a single attacker source cannot occupy the entire headroom pool.
- Reduce/derive `QUIC_CONNECTION_HANDSHAKE_TIMEOUT` more aggressively for connections showing no progress (e.g., no Handshake packet received within a short sub-timeout) instead of allowing the full 2s grace period unconditionally.
- Consider leveraging QUIC's built-in address-validation/retry mechanism to require proof of reachability before committing a tracked slot.

### Proof of Concept
Integration test plan (streamer crate, using `quinn`):
1. Start a real QUIC server via `run_server`/`ClientConnectionTracker`/`SimpleQos` with a small `max_staked_connections` (e.g., 4), giving `max_concurrent_connections() == 5`.
2. From N = 5 separate unstaked client `Endpoint`s (no valid stake, arbitrary self-signed certs), initiate connections but intentionally stall handshake completion (e.g., drop/delay the client Handshake flight after sending Initial, using a custom `quinn::EndpointConfig`/UDP socket wrapper that withholds packets).
3. While those 5 are pending, attempt a 6th connection from a legitimate (would-be staked) client.
4. Assert:
   - `stats.refused_connections_too_many_open_connections` (or equivalent stat) increments for the 6th connection attempt.
   - The 6th connection is not admitted until one of the stalled trackers times out (~`QUIC_CONNECTION_HANDSHAKE_TIMEOUT` ≈ 2s later), demonstrating time-to-admit ≈ full timeout window.
   - Repeat the stall+reconnect cycle in a loop to show `refused_connections_too_many_open_connections` keeps incrementing continuously, confirming sustained denial of service to legitimate connections.

### Citations

**File:** streamer/src/nonblocking/simple_qos.rs (L310-348)
```rust
            match conn_context.peer_type() {
                ConnectionPeerType::Staked(stake) => {
                    let mut connection_table_l = self.staked_connection_table.lock().await;

                    if connection_table_l.total_size >= self.config.max_staked_connections {
                        let num_pruned =
                            connection_table_l.prune_random(PRUNE_RANDOM_SAMPLE_SIZE, stake);

                        debug!(
                            "Pruned {} staked connections to make room for new staked connection \
                             from {}",
                            num_pruned, conn_context.remote_address,
                        );
                        self.stats
                            .num_evictions_staked
                            .fetch_add(num_pruned, Ordering::Relaxed);
                        update_open_connections_stat(&self.stats, &connection_table_l);
                    }

                    if connection_table_l.total_size < self.config.max_staked_connections
                        && let Ok((last_update, cancel_connection, stream_counter)) = self
                            .cache_new_connection(
                                client_connection_tracker,
                                connection,
                                connection_table_l,
                                conn_context,
                            )
                    {
                        self.stats
                            .connection_added_from_staked_peer
                            .fetch_add(1, Ordering::Relaxed);
                        conn_context.last_update = last_update;
                        conn_context.stream_counter = Some(stream_counter);
                        return Some(cancel_connection);
                    }
                    None
                }
                ConnectionPeerType::Unstaked => None,
            }
```

**File:** streamer/src/nonblocking/simple_qos.rs (L422-425)
```rust
    fn max_concurrent_connections(&self) -> usize {
        // Allow 25% more connections than required to allow for handshake
        self.config.max_staked_connections * 5 / 4
    }
```
