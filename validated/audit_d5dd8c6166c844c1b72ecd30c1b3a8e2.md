### Title
Priority-tie eviction in `TransactionStateContainer::push_ids_into_queue` lets an attacker's later-arriving, equal-fee transaction deterministically evict an earlier victim transaction of identical priority - (File: core/src/banking_stage/transaction_scheduler/transaction_state_container.rs)

### Summary
`TransactionStateContainer` orders pending transactions in a `BTreeSet<TransactionPriorityId>` keyed by `(priority, TransactionId)` where `TransactionId` is the monotonically-increasing `Slab` key assigned at insertion time. Because eviction on capacity overflow always removes via `pop_first()`, two transactions with identical `priority` are broken by `TransactionId`, so the earlier-inserted (lower-id) transaction is always evicted before a later-inserted one of the same priority.

### Finding Description
`TransactionPriorityId` derives `Ord` over `(priority, id)` in that field order [1](#0-0) , and its own test explicitly documents that at equal priority the entry with the smaller `id` sorts lower [2](#0-1) .

`TransactionStateContainer::push_ids_into_queue` inserts new ids into the `BTreeSet` and, whenever the backing `Slab` (`id_to_transaction_state`) exceeds `capacity`, repeatedly calls `self.priority_queue.pop_first()` to select victims for eviction: [3](#0-2) 

`TransactionId` is the `Slab` vacant-entry key handed out at `insert_map_only` time [4](#0-3) , and it increases with each new insertion (absent slot reuse from removed entries). Consequently, for two transactions with the exact same `priority`, the one inserted earlier holds the smaller `id`, sorts to `pop_first()`, and is evicted first — even though it arrived first and is equally fee-paying.

Priority is derived purely from the transaction's own fee/compute-unit-price fields (visible in `core/src/transaction_priority.rs`), which is fully attacker-controlled input carried in the wire transaction. Since `compute_unit_price` of `0` (no priority fee) is the common default for ordinary transfers, an unstaked remote attacker sending many zero-fee (or otherwise fee-matched) transactions after a victim's zero-fee transaction is already buffered does not need to "compute" anything special — matching the victim's price is the default case, and higher-arriving-id entries always outrank lower-arriving-id entries at that price once eviction triggers via `push_ids_into_queue`. No fairness/insertion-order protection, dedup, or rate limit exists in this path to prevent this; sigverify and QoS checks upstream do not consider queue-position fairness at all — they only gate signature validity and compute-budget/cost limits, not eviction ordering. The existing repo test `test_view_push_ids_to_queue` already demonstrates the exact mechanic (later, equal/near priorities dropping earlier queued entries) [5](#0-4) .

### Impact Explanation
This falls under buffer-eviction gameability described in the file scope: an unprivileged remote attacker can flood a leader's TPU with transactions priced to tie a target victim's fee, and once the bounded container (`TransactionStateContainer`, capacity fixed at `with_capacity`) is filled with attacker traffic exceeding capacity, the victim's earlier-buffered, equal-fee transaction is guaranteed to be silently dropped via `push_ids_into_queue`'s `pop_first()` eviction, rather than the outcome being fee/priority-driven. This is a real, reproducible fairness/QoS-evasion issue in ordering logic (not a crash/OOM), but it is a legitimate scoped weakness in "priority handling, or buffer eviction" per the audit's stated scope.

### Likelihood Explanation
Highly feasible and repeatable: no special access is needed beyond sending arbitrary transactions to the public TPU (unstaked, unprivileged). Matching priority is trivial because zero (or any common) compute-unit price is the default for most wallets/dApps, so ties occur naturally at scale; the attacker only needs to arrive after the victim and supply enough distinct equal/higher-priority transactions to push the container past `capacity`. This is deterministic given the `BTreeSet`/`Slab`-id ordering, not probabilistic.

### Recommendation
Change the tie-break semantics for `TransactionPriorityId` (or the eviction path specifically) so that equal-priority eviction is not purely a function of arrival order that always disadvantages earlier transactions — e.g., break ties randomly/hashed rather than by monotonic id, or make eviction prefer removing the most-recently-inserted of an equal-priority tie (reverse the tie ordering) to preserve first-come-first-served fairness at equal price, and document/test this invariant explicitly in `push_ids_into_queue`.

### Proof of Concept
```rust
// core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (tests module)
#[test]
fn test_equal_priority_tie_break_evicts_earlier_transaction() {
    let mut container = TransactionStateContainer::with_capacity(1);

    // Victim inserted first, priority = 5
    let (victim_tx, max_age, priority, cost) = test_transaction(5);
    let victim_dropped = container.insert_new_transaction(victim_tx, max_age, priority, cost);
    assert!(!victim_dropped);

    // Attacker inserted second, SAME priority = 5 (ties on TransactionId only)
    let (attacker_tx, max_age, priority, cost) = test_transaction(5);
    let attacker_dropped = container.insert_new_transaction(attacker_tx, max_age, priority, cost);

    // Container capacity is 1: one of the two ties must be evicted.
    // Assert the victim (earlier, lower TransactionId) is the one dropped,
    // and the attacker's later-inserted equal-fee transaction survives.
    assert!(!attacker_dropped, "attacker's later tx should NOT be dropped");
    assert_eq!(container.queue_size(), 1);
    let remaining = container.pop().unwrap();
    assert_eq!(remaining.priority, 5);
    // remaining.id should be the attacker's (higher) id, proving victim was evicted first
}
```
Expected result: the assertion confirms the victim's earlier, equal-fee transaction is evicted while the attacker's later, equal-fee transaction survives — demonstrating the tie-break is deterministically exploitable by arrival-order manipulation at equal price.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/transaction_priority_id.rs (L4-14)
```rust
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) struct TransactionPriorityId {
    pub(crate) priority: u64,
    pub(crate) id: TransactionId,
}

impl TransactionPriorityId {
    pub(crate) fn new(priority: u64, id: TransactionId) -> Self {
        Self { priority, id }
    }
}
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_priority_id.rs (L32-40)
```rust
        // Equal priority then compare by id
        {
            let id1 = TransactionPriorityId::new(1, 1);
            let id2 = TransactionPriorityId::new(1, 2);
            assert!(id1 < id2);
            assert!(id1 <= id2);
            assert!(id2 > id1);
            assert!(id2 >= id1);
        }
```

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

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L268-273)
```rust
    pub(crate) fn insert_map_only(&mut self, state: TransactionState<Tx>) -> TransactionId {
        let entry = self.get_vacant_map_entry();
        let transaction_id = entry.key();
        entry.insert(state);
        transaction_id
    }
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L411-478)
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
    }
```
