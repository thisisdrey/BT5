### Title
Data-budget refund on repair-request miss fully cancels the byte cost, leaving guaranteed-miss `WindowIndex`/`HighestWindowIndex`/`Orphan`/`AncestorHashes` repair requests effectively free and unthrottled by CPU/blockstore cost - ([File: core/src/repair/serve_repair.rs])

### Summary
`handle_requests` charges `data_budget` for the maximum possible response size before invoking `handle_repair`, but on a miss it immediately refunds the *entire* charge with `data_budget.add_tokens(max_response_cost)`, netting a zero cost even though a full blockstore lookup (`Blockstore::meta`/`get_data_shred`) was already performed by the `RepairHandler` default methods. Because the byte-budget is the only cost model gating repair-request throughput, an attacker who sends only guaranteed-miss requests pays no byte cost at all for the blockstore reads they trigger, and the only remaining limits are unrelated structural caps (batch size, channel backpressure), not a CPU-aware throttle.

### Finding Description
In `core/src/repair/serve_repair.rs::handle_requests`, tokens are consumed up front: [1](#0-0) 

then `self.handle_repair(...)` is invoked, which dispatches to the default `RepairHandler` trait methods in `core/src/repair/repair_handler.rs`. Every one of `run_window_request`, `run_highest_window_request`, `run_orphan`, and `run_ancestor_hashes` performs the real blockstore lookup (`blockstore().meta(slot)`, `repair_response_packet`, ancestor iteration) *before* deciding to return `None` on a miss: [2](#0-1) [3](#0-2) 

Back in `handle_requests`, when `handle_repair` returns `None` (a miss), the full `max_response_cost` is refunded unconditionally: [4](#0-3) 

The net token delta for a miss is `consume(max_response_cost)` followed by `add_tokens(max_response_cost)` = 0. Since this happens synchronously within the single-threaded `run_listen`/`handle_requests` loop, no other request can observe or benefit from the temporarily-consumed tokens in between. This means `data_budget` — the only cost-accounting mechanism visible in this path — imposes zero effective throttle on a stream of guaranteed-miss requests. The remaining gates are:
- `check_ping_cache` (a one-time per (pubkey, addr) handshake cost, cached for `REPAIR_PING_CACHE_TTL`, trivially satisfied once by an attacker controlling their own socket): [5](#0-4) 
- `MAX_REQUESTS_PER_ITERATION = 1024` and buffered-packet load-shedding, which bound batch size per iteration but not the number of iterations per second: [6](#0-5) 

None of these are CPU-cost-aware; they only bound queue/batch sizes, not the sustained rate of blockstore-read-triggering packets an attacker can push once past ping-cache.

### Impact Explanation
This is a grossly underpriced pre-fee/pre-verification work issue: a cheap, unstaked, unsigned-cost UDP packet forces a real `Blockstore` read (RocksDB `meta`/`get` calls, potentially disk I/O) on the validator's repair-listener thread, and the accounting model that is supposed to bound this cost (the byte `data_budget`) is bypassed entirely for the miss case because consumption and refund cancel out. An attacker can sustain a rate of guaranteed-miss repair requests limited only by their own packet-send rate and the receive-side socket/channel capacity, not by the intended byte-budget of `MAX_BYTES_PER_SECOND = 12_000_000` bytes/sec (`core/src/repair/serve_repair.rs:1384`), causing disproportionate CPU/blockstore load on the `solRepairListen` thread relative to what the budget is meant to permit.

### Likelihood Explanation
Feasible for a fully unprivileged, unstaked remote attacker: they need only (1) complete one repair ping/pong handshake with the target using their own controlled socket address (a normal, one-time step, not requiring any secret or staked identity), then (2) repeatedly send well-formed `RepairProtocol::WindowIndex`/`HighestWindowIndex`/`Orphan`/`AncestorHashes` messages referencing slots/shred indices that are known in advance to miss (e.g., far beyond any `meta.received`, or entirely absent slots). This requires no cluster state manipulation, no staking, and is trivially repeatable at line rate.

### Recommendation
Do not refund the full `max_response_cost` on a miss. Charge a smaller, fixed "lookup cost" per request independent of whether it hits or misses (e.g., only refund `max_response_cost - lookup_cost`), or introduce a separate CPU/work-based cost model (e.g., a per-request fixed token cost for the blockstore lookup itself) that is deducted from `data_budget` regardless of hit/miss outcome, so that guaranteed-miss traffic still consumes budget proportional to the work performed.

### Proof of Concept
Add a unit test alongside `test_run_window_request` in `core/src/repair/serve_repair.rs` tests module:
```rust
#[test]
fn test_miss_requests_do_not_consume_data_budget() {
    let recycler = PacketBatchRecycler::default();
    let ledger_path = get_tmp_ledger_path_auto_delete!();
    let blockstore = Arc::new(Blockstore::open(ledger_path.path()).unwrap());
    // ... construct ServeRepair with StandardRepairHandler over an empty blockstore ...

    const MAX_BYTES_PER_SECOND: u64 = 12_000_000;
    let data_budget = TokenBucket::new(MAX_BYTES_PER_SECOND, MAX_BYTES_PER_SECOND, MAX_BYTES_PER_SECOND as f64);
    let tokens_before = data_budget.current_tokens();

    // Build N guaranteed-miss WindowIndex requests for a slot/shred_index that
    // is far beyond meta.received (or a nonexistent slot).
    let requests: Vec<RepairRequestWithMeta> = (0..10_000)
        .map(|i| make_window_index_request_with_meta(slot, /*shred_index=*/u64::MAX - i))
        .collect();

    let mut stats = ServeRepairStats::default();
    let mut ping_cache = /* pre-populated so check_ping_cache always succeeds */;
    serve_repair.handle_requests(&mut ping_cache, &recycler, requests, &sender, &mut stats, &data_budget, 1);

    // Assertion 1: byte budget is untouched despite 10,000 forced blockstore reads.
    assert_eq!(data_budget.current_tokens(), tokens_before);
    // Assertion 2 (would fail without fix / demonstrates missing CPU throttle):
    // an independent per-second lookup-count or CPU-time counter for this thread
    // should be bounded; currently no such counter exists to assert against.
    assert_eq!(stats.window_index_misses, 10_000);
}
```
Expected result with the current code: `data_budget.current_tokens()` is unchanged after processing 10,000 miss requests (proving zero effective byte cost), while `Blockstore::meta` was invoked 10,000 times — demonstrating that the byte-budget provides no throttling for this workload and that no independent CPU-cost throttle exists to bound it.

### Citations

**File:** core/src/repair/serve_repair.rs (L1173-1210)
```rust
        const MAX_REQUESTS_PER_ITERATION: usize = 1024;
        let mut total_requests = initial_batch.len();

        let socket_addr_space = *self.cluster_info.socket_addr_space();
        let root_bank = self.sharable_banks.root();
        let epoch_staked_nodes = root_bank.epoch_staked_nodes(root_bank.epoch());
        let identity_keypair = self.cluster_info.keypair();
        let my_id = identity_keypair.pubkey();

        let target_max_buffered_packets = if !self.repair_whitelist.read().unwrap().is_empty() {
            4 * MAX_REQUESTS_PER_ITERATION
        } else {
            2 * MAX_REQUESTS_PER_ITERATION
        };

        let mut requests = Vec::<BytesPacket>::with_capacity(64);
        for packet in initial_batch.iter() {
            if is_well_formed_repair_request(&packet, stats) {
                requests.push(packet.to_bytes_packet());
            }
        }
        while let Ok(batch) = requests_receiver.try_recv() {
            total_requests += batch.len();
            for packet in batch.into_iter() {
                if is_well_formed_repair_request(&packet, stats) {
                    requests.push(packet.to_bytes_packet());
                }
            }

            if requests.len() > target_max_buffered_packets {
                // Already exceeded max_buffered_packets. We must be under extreme load.
                // Don't waste time on stale requests and eradicate all buffered packets.
                let drained: usize = requests_receiver.try_iter().map(|batch| batch.len()).sum();
                total_requests += drained;
                stats.dropped_requests_load_shed += drained;
                break;
            }
        }
```

**File:** core/src/repair/serve_repair.rs (L1486-1542)
```rust
    fn check_ping_cache(
        ping_cache: &mut PingCache,
        request: &RepairProtocol,
        from_addr: &SocketAddr,
        identity_keypair: &Keypair,
    ) -> (bool, Option<Packet>) {
        let mut rng = rand::rng();
        let (check, ping) = request
            .sender()
            .map(|&sender| {
                ping_cache.check(
                    &mut rng,
                    identity_keypair,
                    Instant::now(),
                    (sender, *from_addr),
                )
            })
            .unwrap_or_default();
        let ping_pkt = if let Some(ping) = ping {
            match request {
                RepairProtocol::WindowIndex { .. }
                | RepairProtocol::HighestWindowIndex { .. }
                | RepairProtocol::Orphan { .. }
                | RepairProtocol::WindowIndexForBlockId { .. } => {
                    let ping = RepairResponse::Ping(ping);
                    packet_from_data(Some(from_addr), ping).ok()
                }
                RepairProtocol::ParentAndFecSetCount { .. } | RepairProtocol::FecSetRoot { .. } => {
                    let ping = BlockIdRepairResponse::Ping { ping };
                    packet_from_data(Some(from_addr), ping).ok()
                }
                RepairProtocol::AncestorHashes { .. } => {
                    let ping = AncestorHashesResponse::Ping(ping);
                    packet_from_data(Some(from_addr), ping).ok()
                }
                RepairProtocol::Pong(_) => None,
                RepairProtocol::LegacyWindowIndex
                | RepairProtocol::LegacyHighestWindowIndex
                | RepairProtocol::LegacyOrphan
                | RepairProtocol::LegacyWindowIndexWithNonce
                | RepairProtocol::LegacyHighestWindowIndexWithNonce
                | RepairProtocol::LegacyOrphanWithNonce
                | RepairProtocol::LegacyAncestorHashes => {
                    error!("Unexpected legacy request: {request:?}");
                    debug_assert!(
                        false,
                        "Legacy requests should have been filtered out during signature \
                         verification. {request:?}"
                    );
                    None
                }
            }
        } else {
            None
        };
        (check, ping_pkt)
    }
```

**File:** core/src/repair/serve_repair.rs (L1564-1573)
```rust
            // we deliberately consume early assuming that request succeeds,
            // if it does we will refund the unused tokens
            let max_response_cost = request.max_response_bytes() * byte_cost_multiplier;
            if data_budget
                .consume_tokens(max_response_cost as u64)
                .is_err()
            {
                stats.dropped_requests_outbound_bandwidth += 1;
                continue;
            }
```

**File:** core/src/repair/serve_repair.rs (L1587-1592)
```rust
            stats.processed += 1;
            let Some(rsp) = self.handle_repair(recycler, &from_addr, request, stats, ping_cache)
            else {
                data_budget.add_tokens(max_response_cost as u64);
                continue;
            };
```

**File:** core/src/repair/repair_handler.rs (L107-130)
```rust
    fn run_highest_window_request(
        &self,
        recycler: &PacketBatchRecycler,
        from_addr: &SocketAddr,
        slot: Slot,
        highest_index: u64,
        nonce: Nonce,
    ) -> Option<PacketBatch> {
        // Try to find the requested index in one of the slots
        let meta = self.blockstore().meta(slot).ok()??;
        if meta.received > highest_index {
            // meta.received must be at least 1 by this point
            let packet = self.repair_response_packet(slot, meta.received - 1, from_addr, nonce)?;
            return Some(
                RecycledPacketBatch::new_with_recycler_data(
                    recycler,
                    "run_highest_window_request",
                    vec![packet],
                )
                .into(),
            );
        }
        None
    }
```

**File:** core/src/repair/repair_handler.rs (L141-159)
```rust
    fn run_ancestor_hashes(
        &self,
        recycler: &PacketBatchRecycler,
        from_addr: &SocketAddr,
        slot: Slot,
        nonce: Nonce,
    ) -> Option<PacketBatch> {
        let ancestor_slot_hashes = if self.blockstore().is_duplicate_confirmed(slot) {
            let ancestor_iterator = AncestorIteratorWithHash::from(
                AncestorIterator::new_inclusive(slot, self.blockstore()),
            );
            ancestor_iterator.take(MAX_ANCESTOR_RESPONSES).collect()
        } else {
            // If this slot is not duplicate confirmed, return nothing
            vec![]
        };
        let response = AncestorHashesResponse::Hashes(ancestor_slot_hashes);
        create_response_packet_batch(recycler, &response, from_addr, nonce, "run_ancestor_hashes")
    }
```
