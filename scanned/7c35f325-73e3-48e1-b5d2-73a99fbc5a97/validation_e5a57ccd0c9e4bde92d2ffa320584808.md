### Title
On-demand assigner `MigrateV1ToV2` silently drops paid-for coretime orders when the v1 backlog exceeds v2 queue capacity - ([File: polkadot/runtime/parachains/src/on_demand/migration.rs])

### Summary
The `UncheckedMigrateToV2::on_runtime_upgrade` routine that migrates the on-demand assigner's `FreeEntries`/`AffinityEntries` (v1) storage into the new `OrderStatus.queue` (v2) silently truncates the order list once the new bounded queue is full, whereas the corresponding `pre_upgrade`/`post_upgrade` invariant check (that would catch and fail on exactly this condition) only executes under `#[cfg(feature = "try-runtime")]`/`test` and is never invoked in production. Any signed account can inflate the v1 backlog arbitrarily far past the (much smaller) v2 capacity simply by placing many on-demand orders (`place_order_allow_death`/`place_order_keep_alive`), so a permissionless, fee-paying action determines whether a subsequent runtime upgrade permanently and silently destroys already-paid order slots.

### Finding Description
The v1 on-demand queue format bounds the queue index space to `ON_DEMAND_MAX_QUEUE_MAX_SIZE = 1_000_000_000` [1](#0-0) , which is effectively unbounded compared with the runtime's practical order throughput, and orders are placed through the normal permissionless on-demand extrinsics.

When the pallet is migrated to v2, `UncheckedMigrateToV2::on_runtime_upgrade` collects every order from `FreeEntries` and every per-core `AffinityEntries` heap into `all_orders`, sorts them, and pushes them one-by-one into the new bounded `OrderStatus.queue`: [2](#0-1) 

If `order_status.queue.try_push(...)` fails because the new bounded queue is full, the code does not error, does not retry, and does not preserve the un-migrated orders anywhere - it just logs a warning and `break`s out of the loop, discarding every remaining order permanently:
```
if let Err(para_id) = order_status.queue.try_push(now, old_order.para_id) {
    log::warn!(... "queue full, stopping migration of remaining orders" ...);
    break;
}
```
This is a fail-open path: `on_runtime_upgrade` (the function that actually executes during a live runtime upgrade) returns a `Weight` unconditionally and never returns an error, so nothing about this drop is visible on-chain to the runtime executor.

The invariant that is *supposed* to prevent this (checking `total_orders > ON_DEMAND_MAX_QUEUE_MAX_SIZE` and rejecting the migration) exists only in `pre_upgrade`, and `pre_upgrade`/`post_upgrade` are gated by `#[cfg(feature = "try-runtime")]` [3](#0-2)  and `#[cfg(feature = "try-runtime")]` on the trait impl [4](#0-3) . In a live chain, `try-runtime` checks are not executed as part of the actual on-chain upgrade; only `on_runtime_upgrade()` runs. Also note the check in `pre_upgrade` compares against the *old* v1 capacity (`ON_DEMAND_MAX_QUEUE_MAX_SIZE`, 1e9), not the *new* v2 `OrderStatus.queue` bound — so even the try-runtime dry-run check is checking the wrong limit and would not reliably catch a queue that overflows the smaller v2 bound.

The pallet's own test `queue_full_handling` at the bottom of the file demonstrates and accepts this exact behavior — "some orders may be dropped if queue is full" — showing the maintainers were aware silent dropping can occur, but that it is treated as a benign/expected outcome rather than a bug to prevent: [5](#0-4) 

This directly parallels the Optimism M-9 pattern: a public/unprivileged entry point (the LegacyMessagePasser call / here, `place_order_*` extrinsics) can populate state whose format/size is implicitly trusted by a later, one-shot, unrecoverable migration process; a strict-format invariant check exists but is not enforced on the real migration path, so violating that invariant (oversized backlog vs. new format's capacity) causes state to be silently lost rather than the migration failing safely or preserving the excess data.

### Impact Explanation
Orders placed via `place_order_allow_death`/`place_order_keep_alive` reserve/charge the caller's balance for on-demand coretime (a paid execution slot). If those orders are silently dropped during the v1→v2 migration because the backlog exceeded the new bounded queue capacity, payers lose the coretime slots they already paid for with no refund and no on-chain trace connecting the loss to the migration (only a log line, which is off-chain and not part of consensus state). This is a permanent, protocol-level loss of purchased on-chain resources/funds triggered by an unprivileged, permissionless action (placing many on-demand orders before the network performs a scheduled runtime upgrade) combined with a maintainer-triggered but non-malicious event (a normal runtime upgrade). It matches the "permanent user-fund lock" / "message queues ... must only advance after decode, dispatch, execution and settlement succeed atomically" impact categories, since the queue's storage version silently advances to v2 while dropping unprocessed paid work.

### Likelihood Explanation
The precondition (v1 backlog exceeding the new v2 bounded capacity) requires the attacker to know or guess that the v2 queue bound is materially smaller than the v1 conceptual limit and to place enough orders before the migration executes. Placing that many orders costs on-demand fees, but since the point of the attack is precisely to sacrifice a portion of those fees/slots to grief other users' pending orders (or simply happens organically on a busy relay chain with a large legitimate backlog at upgrade time), this can occur without any privileged access, malicious validator, or governance abuse. The bug is latent until the one-time v1→v2 migration is executed on a live chain, at which point it is silent and unrecoverable.

### Recommendation
- Change `on_runtime_upgrade` to make the capacity-overflow condition unmissable and safe: either bound the number of orders admissible to migrate by refusing the migration and re-scheduling it (e.g., processing in batched/multi-block steps akin to `SteppedMigration`) instead of dropping the tail, or explicitly refund/emit an on-chain event per dropped order so token/coretime accounting stays auditable and correctable.
- Move the "would lose orders" capacity check out of `try-runtime`-only code and enforce it (or an equivalent safeguard) in the real `on_runtime_upgrade` path, comparing against the *new* v2 capacity, not the old v1 constant.
- If truncation is unavoidable, deterministically pick which orders are dropped (e.g., refund the paid amount for dropped orders) rather than silently discarding paid orders.

### Proof of Concept
1. On a chain still running on-demand v1 storage, an attacker (or many independent ordinary users) call `on_demand::place_order_allow_death`/`place_order_keep_alive` repeatedly, filling `FreeEntries`/`AffinityEntries` with far more entries than the new v2 `OrderStatus.queue` bound will hold (the v1 constant `ON_DEMAND_MAX_QUEUE_MAX_SIZE` is 1e9, orders of magnitude larger than any practical bounded v2 queue) [1](#0-0) .
2. Governance performs the scheduled runtime upgrade containing `MigrateV1ToV2`. In production only `UncheckedOnRuntimeUpgrade::on_runtime_upgrade` executes (no `pre_upgrade`/`post_upgrade` invariant enforcement) [6](#0-5) .
3. Inside `on_runtime_upgrade`, once `order_status.queue.try_push` starts returning `Err`, the loop `break`s, discarding all remaining (already-paid) orders with only a `log::warn!` [7](#0-6) .
4. Storage version advances to 2, the v1 storages are cleared/taken, and the dropped orders are unrecoverable — payers never get their on-demand coretime slot nor a refund, and no on-chain state records that a drop happened.
5. This is directly reproducible by adapting the pallet's own `queue_full_handling` test, which already confirms (and accepts) that "some orders may be dropped if queue is full" without any error being raised [5](#0-4) .

### Citations

**File:** polkadot/runtime/parachains/src/on_demand/migration.rs (L32-34)
```rust
	/// Old value of ON_DEMAND_MAX_QUEUE_MAX_SIZE from v1.
	const ON_DEMAND_MAX_QUEUE_MAX_SIZE: u32 = 1_000_000_000;

```

**File:** polkadot/runtime/parachains/src/on_demand/migration.rs (L146-183)
```rust
#[cfg(any(feature = "try-runtime", test))]
impl<T: Config> UncheckedMigrateToV2<T> {
	pub fn pre_upgrade() -> Result<alloc::vec::Vec<u8>, sp_runtime::TryRuntimeError> {
		let old_queue_status = v1::QueueStatus::<T>::get();
		let free_entries = v1::FreeEntries::<T>::get().unwrap_or_default();
		let affinity_keys: alloc::vec::Vec<_> = v1::AffinityEntries::<T>::iter_keys().collect();

		let mut total_orders = free_entries.len();
		for core_idx in affinity_keys.iter() {
			total_orders += v1::AffinityEntries::<T>::get(core_idx).unwrap_or_default().len();
		}

		let affinity_count = v1::ParaIdAffinity::<T>::iter().count();

		log::info!(
			target: LOG_TARGET,
			"Before migration: {} total orders ({} free, {} in affinity queues), {} affinity mappings, traffic: {:?}",
			total_orders,
			free_entries.len(),
			total_orders - free_entries.len(),
			affinity_count,
			old_queue_status.as_ref().map(|s| s.traffic)
		);

		// Check that queue won't overflow during migration
		if total_orders > polkadot_primitives::ON_DEMAND_MAX_QUEUE_MAX_SIZE as usize {
			log::error!(
				target: LOG_TARGET,
				"Migration would lose orders: {} total orders exceeds V2 capacity of {}",
				total_orders,
				polkadot_primitives::ON_DEMAND_MAX_QUEUE_MAX_SIZE
			);
			return Err("Too many orders to migrate - queue capacity exceeded".into());
		}

		Ok((total_orders as u32, affinity_count as u32, old_queue_status.map(|s| s.traffic))
			.encode())
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/migration.rs (L237-308)
```rust
impl<T: Config> UncheckedOnRuntimeUpgrade for UncheckedMigrateToV2<T> {
	fn on_runtime_upgrade() -> Weight {
		let mut weight: Weight = Weight::zero();

		let now = frame_system::Pallet::<T>::block_number();
		let old_queue_status = v1::QueueStatus::<T>::take().unwrap_or_else(|| v1::OldQueueStatus {
			traffic: T::TrafficDefaultValue::get(),
			..Default::default()
		});
		weight.saturating_accrue(T::DbWeight::get().reads_writes(1, 1));

		// Collect all orders from both free and affinity queues
		let mut all_orders = alloc::vec::Vec::new();

		// Collect from free entries (1 read + 1 write via take())
		let free_entries = v1::FreeEntries::<T>::take().unwrap_or_default();
		weight.saturating_accrue(T::DbWeight::get().reads_writes(1, 1));
		for order in free_entries.into_iter() {
			all_orders.push(order);
		}

		// Collect from all affinity entries using drain for efficiency (reads + removes in one
		// op)
		let mut affinity_count = 0u64;
		for (_core_idx, affinity_heap) in v1::AffinityEntries::<T>::drain() {
			affinity_count += 1;
			for order in affinity_heap.into_iter() {
				all_orders.push(order);
			}
		}
		// drain() performs reads + writes in one operation
		weight.saturating_accrue(T::DbWeight::get().reads_writes(affinity_count, affinity_count));

		// Sort by QueueIndex to preserve order (ascending)
		all_orders.sort_by_key(|o| o.idx);

		// Drop ParaIdAffinity storage
		let affinity_count = v1::ParaIdAffinity::<T>::iter().count();
		let _ = v1::ParaIdAffinity::<T>::clear(u32::MAX, None);
		weight.saturating_accrue(
			T::DbWeight::get().reads_writes(affinity_count as u64, affinity_count as u64),
		);

		// Build new OrderStatus
		super::pallet::OrderStatus::<T>::mutate(|order_status| {
			// Preserve the traffic value
			order_status.traffic = old_queue_status.traffic;

			// Add all orders to the new queue
			for old_order in all_orders.iter() {
				if let Err(para_id) = order_status.queue.try_push(now, old_order.para_id) {
					log::warn!(
						target: LOG_TARGET,
						"Failed to migrate order for para_id {:?} - queue full, stopping migration of remaining orders",
						para_id
					);
					// Queue is full, no point trying to add more orders
					break;
				}
			}
		});
		weight.saturating_accrue(T::DbWeight::get().reads_writes(1, 1));

		log::info!(
			target: LOG_TARGET,
			"Migrated on demand assigner storage to v2: {} orders migrated, {} affinity entries removed",
			all_orders.len(),
			affinity_count
		);

		weight
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/migration.rs (L310-318)
```rust
	#[cfg(feature = "try-runtime")]
	fn pre_upgrade() -> Result<alloc::vec::Vec<u8>, sp_runtime::TryRuntimeError> {
		Self::pre_upgrade()
	}

	#[cfg(feature = "try-runtime")]
	fn post_upgrade(state: alloc::vec::Vec<u8>) -> Result<(), sp_runtime::TryRuntimeError> {
		Self::post_upgrade(state)
	}
```

**File:** polkadot/runtime/parachains/src/on_demand/migration.rs (L588-622)
```rust
	#[test]
	fn queue_full_handling() {
		new_test_ext(MockGenesisConfig::default()).execute_with(|| {
			let _now = frame_system::Pallet::<Test>::block_number();

			// Try to add more orders than the queue can hold
			let mut free_queue = BinaryHeap::new();

			// Add many orders (queue might have a limit)
			for i in 0..1000 {
				free_queue.push(v1::OldEnqueuedOrder {
					para_id: ParaId::from(i),
					idx: v1::QueueIndex(i),
				});
			}

			v1::FreeEntries::<Test>::put(free_queue);

			let old_status = v1::OldQueueStatus::default();
			v1::QueueStatus::<Test>::put(old_status);

			StorageVersion::new(1).put::<on_demand::Pallet<Test>>();

			// Run migration - should not panic even if queue is full
			let state =
				UncheckedMigrateToV2::<Test>::pre_upgrade().expect("pre_upgrade should succeed");
			let _weight = UncheckedMigrateToV2::<Test>::on_runtime_upgrade();
			UncheckedMigrateToV2::<Test>::post_upgrade(state).expect("post_upgrade should succeed");

			// Verify migration completed (some orders may be dropped if queue is full)
			let new_status = on_demand::pallet::OrderStatus::<Test>::get();
			// Just verify it doesn't panic and creates some queue
			assert!(new_status.queue.len() > 0);
		});
	}
```
