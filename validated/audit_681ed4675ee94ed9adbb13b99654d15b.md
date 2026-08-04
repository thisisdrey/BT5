### Title
`BookStateFor` entries in `pallet-message-queue` are never removed once a queue is fully drained, causing permanent unbounded storage growth analogous to the un-pruned `delegated[]` map — (File: `substrate/frame/message-queue/src/lib.rs`)

### Summary
The Aleo report's core defect is a storage map whose entries are only ever inserted or updated, never deleted, even once their logical value drops to zero/nothing — allowing an attacker to permanently grow chain state through ordinary, cheap operations. The direct structural analog in this repository is `BookStateFor<T>` in `pallet-message-queue`: a `StorageMap<MessageOriginOf<T>, BookState<...>>` that is created the first time a `MessageOrigin` enqueues any message, and is **never removed** for the lifetime of the chain, even after the queue is fully drained.

### Finding Description
`BookStateFor` is declared as a plain `StorageMap` (no `remove` call exists anywhere for it in the pallet): [1](#0-0) 

Every time a message is enqueued for an origin that doesn't yet have a book, `do_enqueue_messages` inserts a fresh `BookState` and knits it into the ready ring: [2](#0-1) 

When the queue drains to empty, `service_queue` "unknits" it from the `ReadyRing` (so it's no longer walked during servicing) but still writes the (now-empty) `BookState` back with `insert`, never `remove`: [3](#0-2) 

`sweep_queue` behaves the same way — it unknits the ring but keeps the book record permanently: [4](#0-3) 

This is explicitly confirmed by the pallet's own test, which documents "the book still exists" after a full sweep: [5](#0-4) 

Exactly like Aleo's `delegated[]`, the entry is retained forever once created — there is no reaping/pruning path for `BookStateFor` regardless of how many times a queue is emptied. The severity in a Substrate chain depends entirely on the `MessageOrigin` key space used by the runtime config: for `AggregateMessageOrigin::Sibling(ParaId)` / `Ump(UmpQueueId::Para(ParaId))` variants used in Cumulus/relay-chain runtimes, or `Snowbridge(ChannelId)` / `SnowbridgeV2(H256)` variants used by Snowbridge, each distinct key that is ever used to enqueue one message leaves a permanent `BookStateFor` entry: [6](#0-5) [7](#0-6) 

For the `SnowbridgeV2(H256)` variant in particular, the key is an arbitrary 256-bit hash rather than a governance-assigned `ParaId`/`ChannelId`, so the practical bound on how many distinct `BookStateFor` entries can be created depends on how permissively that hash is derived upstream in the V2 outbound-queue message-id/topic generation — a much larger and potentially less access-controlled key space than `ParaId`.

### Impact Explanation
Even though `ReadyRing` prevents empty books from being iterated during normal block servicing (so this is not a per-block O(n) scan like the Aleo `delegated[]` example), the un-pruned `BookStateFor` map still represents permanent, unbounded state growth driven by ordinary usage. This directly matches the accepted "public underpriced work that degrades block production" impact class: each new distinct origin that is ever used costs the attacker/user only the price of sending one cheap message, but leaves a permanent storage-trie entry with no cleanup mechanism, growing the state size, PoV weight, and full-node disk usage indefinitely. `do_try_state` in this same pallet iterates all of `BookStateFor` for sanity checks, so a bloated map also increases verification cost: [8](#0-7) 

### Likelihood Explanation
Likelihood depends on the concrete `MessageOrigin` type wired into a given runtime. Where the key space is governance-restricted (a `ParaId` that must be leased/registered, or a Snowbridge `ChannelId` created by privileged governance action), an unprivileged party cannot cheaply mint unlimited new keys, which reduces this to a slow, bounded background bloat rather than an acute DoS — weaker than the Aleo original, where *any* address (even non-existent ones) could be used freely. Where the origin includes a broader, less access-controlled key (e.g., `SnowbridgeV2(H256)`), the likelihood of an unprivileged actor generating many distinct book entries is higher and should be independently verified against the exact message-id derivation used by that queue's `deliver()`/`validate()` path, which this investigation could not fully trace end-to-end within the available index.

### Recommendation
Add a reaping path for `BookStateFor` entries whose `BookState` is fully empty (`count == 0`, `message_count == 0`, `size == 0`, `ready_neighbours == None`), e.g. removing the entry in `service_queue`/`sweep_queue`/`do_reap_page` once the last page is reaped instead of re-inserting an empty book, mirroring how `pallet-staking`'s `kill_stash`, `pallet-nomination-pools`'s `dissolve_pool`, and `pallet-assets`' `cancel_approval` already remove their zero-value entries rather than leaving them dangling.

### Proof of Concept
1. For any `MessageOrigin` variant whose key an unprivileged actor can influence (verify per-runtime, e.g. `AggregateMessageOrigin::SnowbridgeV2(H256)`), call the corresponding `enqueue_message` path with a distinct origin key `N` times.
2. After each enqueue, allow `on_initialize`/`service_queues` to fully process and drain that queue (single cheap message per origin).
3. Observe via `BookStateFor::<T>::iter().count()` that the map size equals `N` and never decreases, even though every queue is empty (`book.count == 0`), reproducing the same "entries with zero remaining stake are never removed from storage" pattern described in the Aleo `delegated[]` report — confirmed directly by the existing `sweep_queue_works` test which asserts the book persists after full drain: `substrate/frame/message-queue/src/tests.rs:965-999`.

### Citations

**File:** substrate/frame/message-queue/src/lib.rs (L659-662)
```rust
	/// The index of the first and last (non-empty) pages.
	#[pallet::storage]
	pub type BookStateFor<T: Config> =
		StorageMap<_, Twox64Concat, MessageOriginOf<T>, BookState<MessageOriginOf<T>>, ValueQuery>;
```

**File:** substrate/frame/message-queue/src/lib.rs (L1048-1059)
```rust
		// Insert book state for current origin into the ready queue.
		if book_state.ready_neighbours.is_none() {
			match Self::ready_ring_knit(origin) {
				Ok(neighbours) => book_state.ready_neighbours = Some(neighbours),
				Err(()) => {
					defensive!("Ring state invalid when knitting");
				},
			}
		}

		// NOTE: `T::QueueChangeHandler` is called by the caller.
		BookStateFor::<T>::insert(origin, book_state);
```

**File:** substrate/frame/message-queue/src/lib.rs (L1253-1262)
```rust
		let next_ready = book_state.ready_neighbours.as_ref().map(|x| x.next.clone());
		if book_state.begin >= book_state.end {
			// No longer ready - unknit.
			if let Some(neighbours) = book_state.ready_neighbours.take() {
				Self::ready_ring_unknit(&origin, neighbours);
			} else if total_processed > 0 {
				defensive!("Freshly processed queue must have been ready");
			}
		}
		BookStateFor::<T>::insert(&origin, &book_state);
```

**File:** substrate/frame/message-queue/src/lib.rs (L1413-1424)
```rust
	pub fn do_try_state() -> Result<(), sp_runtime::TryRuntimeError> {
		// Checking memory corruption for BookStateFor
		ensure!(
			BookStateFor::<T>::iter_keys().count() == BookStateFor::<T>::iter_values().count(),
			"Memory Corruption in BookStateFor"
		);
		// Checking memory corruption for Pages
		ensure!(
			Pages::<T>::iter_keys().count() == Pages::<T>::iter_values().count(),
			"Memory Corruption in Pages"
		);

```

**File:** substrate/frame/message-queue/src/lib.rs (L1813-1823)
```rust
	fn sweep_queue(origin: MessageOriginOf<T>) {
		if !BookStateFor::<T>::contains_key(&origin) {
			return;
		}
		let mut book_state = BookStateFor::<T>::get(&origin);
		book_state.begin = book_state.end;
		if let Some(neighbours) = book_state.ready_neighbours.take() {
			Self::ready_ring_unknit(&origin, neighbours);
		}
		BookStateFor::<T>::insert(&origin, &book_state);
	}
```

**File:** substrate/frame/message-queue/src/tests.rs (L972-988)
```rust
		let book = BookStateFor::<Test>::get(Here);
		assert!(book.begin != book.end);
		// Removing the service head works
		assert_eq!(ServiceHead::<Test>::get(), Some(Here));
		MessageQueue::sweep_queue(Here);
		assert_ring(&[There, Everywhere(0)]);
		// The book still exits, but has updated begin and end.
		let book = BookStateFor::<Test>::get(Here);
		assert_eq!(book.begin, book.end);

		// Removing something that is not the service head works.
		assert!(ServiceHead::<Test>::get() != Some(Everywhere(0)));
		MessageQueue::sweep_queue(Everywhere(0));
		assert_ring(&[There]);
		// The book still exits, but has updated begin and end.
		let book = BookStateFor::<Test>::get(Everywhere(0));
		assert_eq!(book.begin, book.end);
```

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L44-60)
```rust
pub enum AggregateMessageOrigin {
	/// The message came from the para-chain itself.
	Here,
	/// The message came from the relay-chain.
	///
	/// This is used by the DMP queue.
	Parent,
	/// The message came from a sibling para-chain.
	///
	/// This is used by the HRMP queue.
	Sibling(ParaId),
	/// The message came from a snowbridge channel.
	///
	/// This is used by Snowbridge inbound queue.
	Snowbridge(ChannelId),
	SnowbridgeV2(H256),
}
```

**File:** polkadot/runtime/parachains/src/inclusion/mod.rs (L217-228)
```rust
/// Aggregate message origin for the `MessageQueue` pallet.
///
/// Can be extended to serve further use-cases besides just UMP. Is stored in storage, so any change
/// to existing values will require a migration.
#[derive(
	Encode, Decode, DecodeWithMemTracking, Clone, MaxEncodedLen, Eq, PartialEq, Debug, TypeInfo,
)]
pub enum AggregateMessageOrigin {
	/// Inbound upward message.
	#[codec(index = 0)]
	Ump(UmpQueueId),
}
```
