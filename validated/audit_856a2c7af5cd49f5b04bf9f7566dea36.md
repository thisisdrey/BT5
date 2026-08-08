### Title
Unstaked connection pool amplification via distinct low-stake pubkeys bypassing per-IP connection caps - (File: streamer/src/nonblocking/swqos.rs)

### Finding Description
`SwQos::build_connection_context` (streamer/src/nonblocking/swqos.rs:301-342) computes `peer_type` by looking up the caller's stake via `get_connection_stake`. When a real stake entry is found but `stake_ratio < min_stake_ratio`, the connection is downgraded to `ConnectionPeerType::Unstaked` (lines 323-325), but critically the `remote_pubkey` field is still set to `Some(pubkey)` (line 334) — unlike a genuinely unstaked/unknown peer, whose `remote_pubkey` is set to `None` (line 308).

This `remote_pubkey` is later fed into `ConnectionTableKey::new(remote_addr.ip(), conn_context.remote_pubkey)` inside `cache_new_connection` (swqos.rs:211) and `prune_unstaked_connections_and_add_new_connection`. In `ConnectionTable::try_add_connection` (streamer/src/nonblocking/quic.rs), the per-peer connection cap (`max_connections_per_unstaked_peer`) is enforced against this table key. A `Some(pubkey)` key is distinct per pubkey rather than being scoped by IP, whereas a genuinely-unstaked `None`-keyed peer is scoped/capped purely by IP.

Because a downgraded low-stake connection retains its distinct pubkey-based table key even though it is classified `ConnectionPeerType::Unstaked`, an attacker who obtains multiple distinct on-chain identities each with stake just below `min_stake_ratio` (a heuristic derived purely from `max_streams_per_ms * STREAM_THROTTLING_INTERVAL_MS`, not from any anti-Sybil mechanism) can open connections from a single IP (or few IPs) using N different low-stake pubkeys. Each pubkey gets its own `max_connections_per_unstaked_peer` allotment in the shared `unstaked_connection_table`, whereas a genuinely-unstaked single IP is capped to just one such allotment. This lets a modest amount of aggregate stake, split across many low-value keys, consume N× the per-identity share of the unstaked connection/stream budget that the QoS design intends to give a single unstaked network source.

### Impact Explanation
This is a QoS-evasion / fairness-bypass bug: it allows an attacker to consume a disproportionate share of the shared `unstaked_connection_table` capacity (`DEFAULT_MAX_UNSTAKED_CONNECTIONS`) and the associated unstaked stream-throttle budget by fragmenting into many low-stake identities rather than being constrained like a single unstaked source. This crowds out legitimate unstaked traffic and degrades ingress fairness on a leader's TPU, matching the "QoS evasion" bounty category rather than a memory-safety/consensus bug.

### Likelihood Explanation
Exploitability requires the attacker to control multiple validator identity keys with real (but very small) delegated stake such that `stake_ratio < min_stake_ratio` for each — this is permissionless (splitting a given amount of stake across many identities, no minimum-stake floor is enforced by this code), does not require controlling any existing validator/leader, and does not need gossip or config privileges beyond normal network participation. The more identities an attacker splits stake across, the larger the achievable multiplier, making this practically feasible and repeatable at low capital cost per identity.

### Recommendation
When a staked connection is downgraded to `ConnectionPeerType::Unstaked` due to low `stake_ratio`, also clear `remote_pubkey` to `None` (or otherwise force the connection-table key to be IP-based) so that downgraded low-stake peers are keyed and capped identically to genuinely unstaked peers, i.e., scoped per source IP rather than per pubkey.

### Proof of Concept
Unit test in `streamer/src/nonblocking/swqos.rs` test module:
1. Configure `SwQosConfig::default_for_tests()` with `max_connections_per_unstaked_peer = 1` and a small `max_unstaked_connections` (e.g., 4).
2. Populate `StakedNodes` with total stake `T` and create `N` distinct pubkeys each with stake `s` such that `s / T < min_stake_ratio` (using `max_streams_per_ms` and `STREAM_THROTTLING_INTERVAL_MS` to derive the threshold).
3. Call `build_connection_context` for each of the `N` pubkeys from the *same* remote IP, assert `peer_type() == ConnectionPeerType::Unstaked` for all (confirming downgrade) but `remote_pubkey()` is `Some(distinct_pubkey)` for each.
4. Call `try_add_connection` for each of the `N` connections and assert that all `N` succeed (each occupies its own `max_connections_per_unstaked_peer` slot), demonstrating that `N > 1` connections from one IP were accepted into the unstaked table — exceeding the capacity a single genuinely-unstaked IP (`remote_pubkey = None`) would be permitted, which should be capped at `max_connections_per_unstaked_peer` (1) regardless of how many distinct low-stake identities present themselves from that IP.
5. Contrast by repeating steps 3-4 with `N` connections having `remote_pubkey = None` from the same IP, asserting only 1 succeeds — proving the asymmetry. [1](#0-0) [2](#0-1)

### Citations

**File:** streamer/src/nonblocking/swqos.rs (L205-219)
```rust
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
```

**File:** streamer/src/nonblocking/swqos.rs (L301-342)
```rust
impl QosController<SwQosConnectionContext> for SwQos {
    fn build_connection_context(&self, connection: &Connection) -> SwQosConnectionContext {
        let remote_address = connection.remote_address();
        get_connection_stake(connection, &self.staked_nodes).map_or(
            SwQosConnectionContext {
                peer_type: ConnectionPeerType::Unstaked,
                total_stake: 0,
                remote_pubkey: None,
                in_staked_table: false,
                remote_address,
                stream_counter: None,
                last_update: Arc::new(AtomicU64::new(timing::timestamp())),
            },
            |(pubkey, stake, total_stake)| {
                // The heuristic is that the stake should be large enough to have 1 stream pass through within one throttle
                // interval during which we allow max (MAX_STREAMS_PER_MS * STREAM_THROTTLING_INTERVAL_MS) streams.

                let peer_type = {
                    let max_streams_per_ms = self.staked_stream_load_ema.max_streams_per_ms();
                    let min_stake_ratio =
                        1_f64 / (max_streams_per_ms * STREAM_THROTTLING_INTERVAL_MS) as f64;
                    let stake_ratio = stake as f64 / total_stake as f64;
                    if stake_ratio < min_stake_ratio {
                        // If it is a staked connection with ultra low stake ratio, treat it as unstaked.
                        ConnectionPeerType::Unstaked
                    } else {
                        ConnectionPeerType::Staked(stake)
                    }
                };

                SwQosConnectionContext {
                    peer_type,
                    total_stake,
                    remote_pubkey: Some(pubkey),
                    in_staked_table: false,
                    remote_address,
                    last_update: Arc::new(AtomicU64::new(timing::timestamp())),
                    stream_counter: None,
                }
            },
        )
    }
```
