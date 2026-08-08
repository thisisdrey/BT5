#No Vulnerability found for this question.

**Rationale:** The eviction behavior in `push_ids_into_queue` is not an emergent defect but the intended bounding mechanism of `TransactionStateContainer` — it caps `id_to_transaction_state.len()` at `capacity` by evicting the lowest-priority entries via `priority_queue.pop_first()`/`remove_state()` [1](#0-0) . Each insert/evict pair is `O(log capacity)` on a `BTreeSet`, which is a bounded, constant-ish cost regardless of packet volume, not an unbounded or amortized-unsafe operation as the question presumes.

More importantly, reaching `push_ids_into_queue` at all requires an attacker to survive multiple prior gates that are explicitly designed to bound unprivileged-attacker cost: QUIC per-IP/overall connection rate limiting and stream throttling capping unstaked traffic at ~200 TPS [2](#0-1) [3](#0-2) , packet deduplication and full Ed25519 signature verification in the sigverify stage [4](#0-3) , and — specific to the exact function cited — a mandatory `check_transaction_without_status_cache` and `check_fee_payer_unlocked` validation in `receive_and_buffer.rs` before any transaction is inserted into the container [5](#0-4) . This means an attacker cannot cheaply flood the container with fee-less/duplicate-priority garbage: each accepted insertion already implies a validly signed transaction with a real, unlocked fee payer, which is the intended economic/verification cost gate, not a bypass.

The scoped claim ("CPU wasted proportional to attacker packet rate rather than accepted work") describes the normal, accepted cost model of a fixed-capacity priority buffer feeding a fee market, already exercised and asserted correct by existing tests (`test_priority_queue_capacity`, `test_view_push_ids_to_queue`) [6](#0-5) [7](#0-6) . No panic, unbounded memory, verification bypass, or invalid recorded block results — the container correctly stays bounded at `TOTAL_BUFFERED_PACKETS` capacity as designed, which is the opposite of the alleged invariant violation.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L178-201)
```rust
    fn push_ids_into_queue(
        &mut self,
        priority_ids: impl Iterator<Item = TransactionPriorityId>,
    ) -> usize {
        for id in priority_ids {
            self.priority_queue.insert(id);
        }

        // The number of items in the `id_to_transaction_state` map is
        // greater than or equal to the number of elements in the queue.
        // To avoid the map going over capacity, we use the length of the
        // map here instead of the queue.
        let num_dropped = self
            .id_to_transaction_state
            .len()
            .saturating_sub(self.capacity);

        for _ in 0..num_dropped {
            let priority_id = self.priority_queue.pop_first().expect("queue is not empty");
            self.remove_state(priority_id.id);
        }

        num_dropped
    }
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L377-393)
```rust
    #[test]
    fn test_priority_queue_capacity() {
        let mut container = TransactionStateContainer::with_capacity(1);
        push_to_container(&mut container, 5);

        assert_eq!(container.priority_queue.len(), 1);
        assert_eq!(container.id_to_transaction_state.len(), 1);
        assert_eq!(
            container
                .id_to_transaction_state
                .iter()
                .map(|ts| ts.1.priority())
                .next()
                .unwrap(),
            4
        );
    }
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L411-477)
```rust
    #[test]
    fn test_view_push_ids_to_queue() {
        let mut container = TransactionViewStateContainer::with_capacity(2);

        let reserved_addresses = HashSet::default();
        let packet_parser = |data, priority, cost| {
            let view =
                SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()).unwrap();
            let view = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
                view,
                MessageHash::Compute,
                None,
            )
            .unwrap();
            let view = RuntimeTransaction::<ResolvedTransactionView<_>>::try_new(
                view,
                None,
                &reserved_addresses,
            )
            .unwrap();

            TransactionState::new(view, MaxAge::MAX, priority, cost)
        };

        // Push 2 transactions into the queue so buffer is full.
        for priority in [4, 5] {
            let (transaction, _max_age, priority, cost) = test_transaction(priority);
            let packet = Packet::from_data(None, transaction.to_versioned_transaction()).unwrap();
            let data = Bytes::copy_from_slice(packet.data(..).unwrap());
            let id = container.insert_map_only(packet_parser(data, priority, cost));
            let priority_id = TransactionPriorityId::new(priority, id);
            assert_eq!(
                container.push_ids_into_queue(std::iter::once(priority_id)),
                0
            );
        }

        // Push 5 additional packets in. 5 should be dropped.
        for priority in [10, 11, 12, 1, 2] {
            let (transaction, _max_age, priority, cost) = test_transaction(priority);
            let packet = Packet::from_data(None, transaction.to_versioned_transaction()).unwrap();
            let data = Bytes::copy_from_slice(packet.data(..).unwrap());
            let id = container.insert_map_only(packet_parser(data, priority, cost));
            let priority_id = TransactionPriorityId::new(priority, id);
            assert_eq!(
                container.push_ids_into_queue(std::iter::once(priority_id)),
                1,
            );
        }
        assert_eq!(container.pop().unwrap().priority, 12);
        assert_eq!(container.pop().unwrap().priority, 11);
        assert!(container.pop().is_none());

        // Container now has no items in the queue, but still has 5 items in the map.
        // If we attempt to push additional transactions to the queue, they
        // are rejected regardless of their priority.
        let priority = u64::MAX;
        let (transaction, _max_age, priority, cost) = test_transaction(priority);
        let packet = Packet::from_data(None, transaction.to_versioned_transaction()).unwrap();
        let data = Bytes::copy_from_slice(packet.data(..).unwrap());
        let id = container.insert_map_only(packet_parser(data, priority, cost));
        let priority_id = TransactionPriorityId::new(priority, id);
        assert_eq!(
            container.push_ids_into_queue(std::iter::once(priority_id)),
            1
        );
        assert!(container.pop().is_none());
```

**File:** streamer/src/nonblocking/stream_throttle.rs (L16-19)
```rust
/// Max TPS allowed for unstaked connection
const MAX_UNSTAKED_TPS: u64 = 200;
/// Expected fraction of max TPS to be consumed by unstaked connections
const EXPECTED_UNSTAKED_STREAMS_RATIO: f64 = 0.20;
```

**File:** streamer/src/nonblocking/quic.rs (L346-369)
```rust
            // check overall connection request rate limiter
            if overall_connection_rate_limiter.current_tokens() == 0 {
                stats
                    .connection_rate_limited_across_all
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to overall rate limit.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
            // then perform per IpAddr rate limiting
            if !rate_limiter.is_allowed(&incoming.remote_address().ip()) {
                stats
                    .connection_rate_limited_per_ipaddr
                    .fetch_add(1, Ordering::Relaxed);
                debug!(
                    "Ignoring incoming connection from {} due to per-IP rate limiting.",
                    incoming.remote_address()
                );
                incoming.ignore();
                continue;
            }
```

**File:** core/src/sigverify.rs (L282-344)
```rust
        let (discard_or_dedup_fail, dedup_time_us) =
            measure_us!(deduper::dedup_packets_and_count_discards(
                &state.deduper,
                std::slice::from_mut(&mut batch)
            ));
        state
            .stats
            .total_dedup
            .fetch_add(discard_or_dedup_fail as usize, Ordering::Relaxed);
        state
            .stats
            .total_dedup_time_us
            .fetch_add(dedup_time_us as usize, Ordering::Relaxed);

        if discard_or_dedup_fail as usize == batch_len {
            return true;
        }

        let working_bank = sharable_banks.working();

        if let Some(floor) = state.priority_floor.as_ref() {
            let floor = floor.get();
            if floor > 0 {
                let ((dropped, all_below), priority_floor_time_us) = measure_us!(
                    apply_priority_floor_to_batch(&mut batch, floor, &working_bank)
                );
                state
                    .stats
                    .total_priority_floor_time_us
                    .fetch_add(priority_floor_time_us as usize, Ordering::Relaxed);
                if dropped > 0 {
                    state
                        .stats
                        .total_dropped_below_priority_floor
                        .fetch_add(dropped, Ordering::Relaxed);
                }
                if all_below {
                    // Entire batch went below-floor: nothing left to verify or
                    // forward.
                    return true;
                }
            }
        }

        let enable_tx_v1 = working_bank.feature_set.snapshot().enable_tx_v1;
        let (_, verify_time_us) = measure_us!(sigverify::ed25519_verify_serial(
            &mut batch,
            reject_non_vote,
            enable_tx_v1,
        ));
        let num_valid_packets = sigverify::count_valid_packets(std::iter::once(&batch));
        state
            .stats
            .total_valid_packets
            .fetch_add(num_valid_packets, Ordering::Relaxed);
        state
            .stats
            .total_verify_time_us
            .fetch_add(verify_time_us as usize, Ordering::Relaxed);

        if num_valid_packets == 0 {
            return true;
        }
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L312-342)
```rust
            // Check blockhash transaction age is ok, or nonce transaction has a valid nonce.
            // Only a fully validated nonce address can be used for priority queue eviction.
            let validated_nonce_address = match working_bank.check_transaction_without_status_cache(
                state.transaction(),
                working_bank.max_processing_age(),
                &mut error_counters,
            ) {
                // Valid nonce transaction
                Ok(Some(nonce_address)) => Some(nonce_address),

                // Valid blockhash transaction
                Ok(None) => None,

                // Invalid
                Err(ref err) => {
                    receiving_stats.add_transaction_error(err);
                    continue;
                }
            };

            // Check the transaction's fee-payer validates.
            if let Err(_err) = Consumer::check_fee_payer_unlocked(
                working_bank,
                state.transaction(),
                &mut error_counters,
            ) {
                receiving_stats.num_dropped_on_fee_payer += 1;
                continue;
            };

            let transaction_id = container.insert_map_only(state);
```
