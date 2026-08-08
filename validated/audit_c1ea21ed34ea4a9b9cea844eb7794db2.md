### Title
Unstaked/non-whitelisted repair requests are dispatched to `RepairHandler` with no per-source rate limiter, only a global bandwidth budget - ([File: core/src/repair/serve_repair.rs])

### Summary
`ServeRepair::handle_repair` dispatches every well-formed, signed `RepairProtocol` request directly into `RepairHandler::run_window_request`/`run_orphan`/`run_ancestor_hashes`/etc. regardless of stake or `serve_repair_whitelist` membership. Admission is gated only by signature validity, a stateful ping/pong handshake, and a single **global** `TokenBucket` bandwidth budget shared by every requester - there is no per-IP/per-pubkey keyed limiter analogous to the QUIC unstaked-connection limiter or gossip's `pull_request_budget: KeyedRateLimiter<IpAddr>`.

### Finding Description
In `handle_repair` (`core/src/repair/serve_repair.rs:871-1044`), every `RepairProtocol` variant (`WindowIndex`, `HighestWindowIndex`, `Orphan`, `AncestorHashes`, `ParentAndFecSetCount`, `FecSetRoot`, `WindowIndexForBlockId`) is unconditionally routed to the corresponding `RepairHandler` method: [1](#0-0) 
None of these match arms check `stake` or `whitelisted` - those fields exist on `RepairRequestWithMeta` (`core/src/repair/serve_repair.rs:735-740`) but are only used for stats and, under overload, for sort priority: [2](#0-1) 
The only gates before `handle_repair` is invoked are:
1. `verify_signed_packet` (`core/src/repair/serve_repair.rs:1430-1484`) - requires the request be signed by *the sender's own keypair*, checks `recipient == my_id` and time skew. An attacker trivially generates a fresh keypair and satisfies this without any stake.
2. `check_ping_cache`/`PingCache::check` (`core/src/repair/serve_repair.rs:1486-1542`, `gossip/src/ping_pong.rs:296-322`) - a stateful ping/pong handshake keyed by `(sender_pubkey, from_addr)`. Once an attacker completes one round-trip (Pong signed with its own keypair), the `(pubkey, addr)` tuple is cached as trusted for up to `REPAIR_PING_CACHE_TTL` (1280s), after which every subsequent request from that tuple passes `check=true` with no further handshake.
3. A single global `TokenBucket` `data_budget` (`MAX_BYTES_PER_SECOND = 12_000_000`) consumed per request in `handle_requests` (`core/src/repair/serve_repair.rs:1544-1614`, `core/src/repair/serve_repair.rs:1384-1401`). This budget is shared by *all* requesters combined - there is no `KeyedRateLimiter<IpAddr>` or per-pubkey bucket for repair, unlike gossip's pull-request path which uses `pull_request_budget: KeyedRateLimiter<IpAddr>` (`gossip/src/cluster_info.rs:187,219-227`).
4. `MAX_REQUESTS_PER_ITERATION = 1024` per `run_listen` cycle, with stake/whitelist used only to decide which requests to *drop* once the batch already exceeds 1024 (`core/src/repair/serve_repair.rs:1250-1254`). Below that threshold, unstaked/non-whitelisted requests are served identically to staked ones.

Consequently, `serve_repair_whitelist` is not an admission-control mechanism for incoming repair requests at all - it only affects (a) priority ordering under contention and (b) `target_max_buffered_packets` sizing (`core/src/repair/serve_repair.rs:1182-1186`). A pubkey absent from the whitelist and holding zero stake can still reach `RepairHandler::run_window_request`, `run_orphan`, and `run_ancestor_hashes`, each of which performs blockstore reads (`self.blockstore().meta(...)`, ancestor-chain iteration in `run_orphan`/`run_ancestor_hashes` via `AncestorIteratorWithHash`) - non-trivial CPU/I/O work per packet.

### Impact Explanation
A single unstaked, non-whitelisted attacker, after one ping/pong round trip, can send a sustained stream of `WindowIndex`/`Orphan`/`AncestorHashes` requests (staying below `MAX_REQUESTS_PER_ITERATION` to avoid the stake-based drop path) and consume the entire shared 12 MB/s repair-response bandwidth budget as well as leader CPU/disk cycles spent on blockstore lookups and ancestor-chain traversal, since there is no per-IP or per-pubkey quota. This is the repair-path analog of bypassing the QUIC unstaked-connection-limit protection: an unprivileged remote party can consume disproportionate leader/validator resources on the repair-serving thread (`solRepairListen`), degrading service (including for staked/whitelisted repair peers) - a resource-exhaustion / DoS-adjacent finding under the "unmetered CPU/bandwidth consumption" bounty category.

### Likelihood Explanation
Fully feasible with an unprivileged, unstaked attacker: generate a keypair, learn the leader's repair socket address (public), complete one ping/pong handshake (no stake required, `Ping`/`Pong` verification only checks the attacker's own signature), then flood signed `RepairProtocol` requests. No gossip/stake/config control is required. Repeatable indefinitely as long as the attacker paces requests below the 1024-per-iteration drop threshold, or simply accepts a fair share of the drop path when contending with legitimate traffic, since the global budget (not per-source) is what is actually exhausted first.

### Recommendation
Introduce a per-source (per-pubkey and/or per-IP) `KeyedRateLimiter` (as already used for gossip pull requests, `gossip/src/cluster_info.rs:219-227`) for the repair-serving path, and gate `handle_repair` dispatch on that limiter in addition to the existing global `TokenBucket`, so that unstaked/non-whitelisted senders cannot individually consume a disproportionate share of the shared repair-serving budget or CPU.

### Proof of Concept
```rust
// core/src/repair/serve_repair.rs (test module)
// Demonstrates that an unstaked, non-whitelisted sender's requests are
// dispatched to RepairHandler exactly like a staked/whitelisted sender's,
// with no distinguishing rate limit besides the shared global TokenBucket.
#[test]
fn test_unstaked_unwhitelisted_reaches_repair_handler() {
    agave_logger::setup();
    let ledger_path = get_tmp_ledger_path_auto_delete!();
    let blockstore = Arc::new(Blockstore::open(ledger_path.path()).unwrap());
    let (shreds, _) = make_many_slot_entries(0, 3, 5);
    blockstore.insert_shreds(shreds, false).unwrap();

    let handler = StandardRepairHandler::new(blockstore.clone());
    let recycler = PacketBatchRecycler::default();

    // Attacker: freshly generated keypair, zero stake, absent from whitelist.
    // handle_repair has no stake/whitelist check on this dispatch path.
    let rv = handler.run_orphan(&recycler, &socketaddr_any!(), 2, 5, /*nonce=*/1);
    assert!(rv.is_some(), "unstaked/unwhitelisted request is served identically");

    // Confirm decode_request marks stake=0/whitelisted=false but does not reject:
    // (see ServeRepair::decode_request, core/src/repair/serve_repair.rs:1053-1088)
    // -> RepairRequestWithMeta{stake: 0, whitelisted: false} is still forwarded
    //    into handle_requests -> handle_repair -> RepairHandler::run_orphan.
}
```
Extend with an integration test spinning up `ServeRepair::listen` with an empty `serve_repair_whitelist` and an `epoch_staked_nodes` map excluding the attacker's pubkey, then assert repeated signed `RepairProtocol::Orphan`/`WindowIndex` requests from a single attacker socket continue to receive full-size responses and drain `data_budget` (via `ServeRepairStats::total_response_bytes_unstaked`) at the same rate as a staked peer would, with no per-source throttling distinct from the global 12 MB/s bucket.

### Citations

**File:** core/src/repair/serve_repair.rs (L882-932)
```rust
                RepairProtocol::WindowIndex {
                    header: RepairRequestHeader { nonce, .. },
                    slot,
                    shred_index,
                } => {
                    stats.window_index += 1;
                    let batch = self.repair_handler.run_window_request(
                        recycler,
                        from_addr,
                        *slot,
                        *shred_index,
                        *nonce,
                    );
                    if batch.is_none() {
                        stats.window_index_misses += 1;
                    }
                    (batch, "WindowIndexWithNonce")
                }
                RepairProtocol::HighestWindowIndex {
                    header: RepairRequestHeader { nonce, .. },
                    slot,
                    shred_index: highest_index,
                } => {
                    stats.highest_window_index += 1;
                    (
                        self.repair_handler.run_highest_window_request(
                            recycler,
                            from_addr,
                            *slot,
                            *highest_index,
                            *nonce,
                        ),
                        "HighestWindowIndexWithNonce",
                    )
                }
                RepairProtocol::Orphan {
                    header: RepairRequestHeader { nonce, .. },
                    slot,
                } => {
                    stats.orphan += 1;
                    (
                        self.repair_handler.run_orphan(
                            recycler,
                            from_addr,
                            *slot,
                            MAX_ORPHAN_REPAIR_RESPONSES,
                            *nonce,
                        ),
                        "OrphanWithNonce",
                    )
                }
```

**File:** core/src/repair/serve_repair.rs (L1250-1254)
```rust
        if decoded_requests.len() > MAX_REQUESTS_PER_ITERATION {
            stats.dropped_requests_low_stake += decoded_requests.len() - MAX_REQUESTS_PER_ITERATION;
            decoded_requests.sort_unstable_by_key(|r| Reverse((r.whitelisted, r.stake)));
            decoded_requests.truncate(MAX_REQUESTS_PER_ITERATION);
        }
```
