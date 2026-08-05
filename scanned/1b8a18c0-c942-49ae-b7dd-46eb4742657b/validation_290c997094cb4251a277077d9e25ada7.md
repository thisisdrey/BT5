### Title
Global per-block `MaxMessagesPerBlock` cap in Snowbridge's outbound queue lets a low-cost sender monopolize the entire bridge's outbound slot and stall all other channels' Ethereum-bound messages - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
Snowbridge's `snowbridge-pallet-outbound-queue` gates commitment of Ethereum-bound messages behind a single, pallet-wide `MessageLeaves` counter that is shared by every channel (every parachain that exports messages through the bridge). Once this shared counter hits `MaxMessagesPerBlock` in a block, `do_process_message` returns `ProcessMessageError::Yield` for *any* channel's next message, causing `pallet-message-queue` to stop making progress on that queue for the block. Because the cap is global rather than per-channel, an attacker who can cheaply and repeatedly trigger tiny outbound Snowbridge messages from any parachain can occupy the entire per-block budget every block, indefinitely deprioritizing legitimate, high-value messages (e.g. asset unlocks/transfers) queued by other channels behind the attacker's spam.

### Finding Description
The outbound queue keeps a single unbounded, un-keyed storage item for all committed message hashes in the current block: [1](#0-0) 

`do_process_message` checks this *global* counter before accepting any message for any channel, and yields (asks to be retried) once the shared limit is reached: [2](#0-1) 

Note the check is against `MessageLeaves::<T>::decode_len()`, which is *not* scoped to `queued_message.channel_id`; it aggregates across every parachain/channel using the bridge. `Nonce` is the only per-channel state: [3](#0-2) 

On the `pallet-message-queue` side, `Yield`/`Unprocessable{permanent:false}` results in `NoProgress`, which causes `service_page` to `break` and stop advancing that queue's page for the current call: [4](#0-3) [5](#0-4) 

Since `MessageLeaves` is reset only at `on_initialize` (once per block) and is shared by all channels: [6](#0-5) 

any account able to trigger outbound Snowbridge exports (e.g. via a reserve-backed transfer/XCM export from any sibling parachain routed through BridgeHub) can flood the shared per-block quota with minimal-value messages every block. Because ordering within `pallet-message-queue`'s ready-ring is round-robin per origin/channel but the *acceptance* gate itself is global, the attacker's channel doesn't even need to be the same channel as the victim's — filling the shared `MaxMessagesPerBlock` slots from any channel(s) starves every other channel's messages from being accepted into a commitment in that block, repeatedly, for as long as the attacker keeps sending.

This is the direct structural analog of the reported bug: `LiquidityReserve.unstakeAllRewardTokens()` is gated on a single shared `coolDownAmount` value that any account can perturb with a small, cheap transaction timed to occupy the "slot," starving legitimate larger requests. Here, the shared gating value is `MessageLeaves` length instead of `coolDownAmount`, and the "slot" is the bridge's per-block message-acceptance budget instead of a cooldown unstake window.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" in the required impact set. A single unprivileged, low-funds account can repeatedly and cheaply monopolize the global outbound message budget for the entire Snowbridge BridgeHub deployment, delaying delivery of legitimate cross-chain asset messages from all other parachains for as long as the spam continues. Because commitment (and thus relayer visibility/delivery to Ethereum) never happens for messages that keep getting `Yield`ed out of a full block, high-value transfers can be stalled indefinitely relative to attacker-controlled traffic, without requiring a malicious relayer, validator, or governance actor — only an ordinary, unprivileged sender of outbound messages.

### Likelihood Explanation
Likelihood depends on how `MaxMessagesPerBlock` is configured and whether triggering an outbound Snowbridge message (via XCM export from a sibling parachain) is priced proportionally to the scarcity of the shared per-block slot; this repository's index does not let me confirm the exact configured value of `MaxMessagesPerBlock` or the fee schedule charged per outbound message versus the bridge's `PricingParameters`/delivery fee logic (`bridges/snowbridge/primitives/core/src/pricing.rs`), so I could not fully verify how cheap sustained spam actually is in production runtime configs. If the fee is not scaled to make filling the block quota costly per unit of "slots consumed," the attack is cheap and repeatable every block indefinitely.

### Recommendation
- Scope the per-block message-acceptance budget per channel (e.g. `MessageLeaves`/count keyed by `channel_id`) rather than as a single pallet-wide counter, so that one channel cannot exhaust capacity for all others.
- Alternatively/additionally, charge outbound-message senders a fee proportional to their consumption of the scarce global per-block slot (congestion-pricing), or apply fair-queuing/weighted round-robin across channels for the `MaxMessagesPerBlock` budget, ensuring no single channel or account can dominate the shared resource across consecutive blocks.

### Proof of Concept
1. Have `MessageLeaves` at 0 at block start (per `on_initialize`).
2. From an attacker-controlled sibling parachain, issue `T::BatchSize::get()`-independent, cheap XCM exports through BridgeHub that each translate into a Snowbridge outbound `send_message`, until `MessageLeaves::<T>::decode_len() == T::MaxMessagesPerBlock::get()`. This is enforced purely by the aggregate `ensure!(MessageLeaves::<T>::decode_len().unwrap_or(0) < T::MaxMessagesPerBlock::get(), Yield)` check shown above, with no per-channel isolation.
3. A victim on a different channel submits a legitimate, high-value outbound message (e.g. an asset transfer to Ethereum) in the same block; `do_process_message` for the victim's message also hits the exhausted global counter and returns `Yield`, so `pallet-message-queue` marks the victim's queue `NoProgress` and defers it.
4. Repeat steps 1–3 every subsequent block (attacker refills the quota as soon as it resets at `on_initialize`), so the victim's message is perpetually pushed behind the attacker's spam, stalling bridge processing for legitimate transfers as long as the attacker sustains the spam.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L225-236)
```rust
	/// Hashes of the ABI-encoded messages in the [`Messages`] storage value. Used to generate a
	/// merkle root during `on_finalize`. This storage value is killed in
	/// `on_initialize`, so should never go into block PoV.
	#[pallet::storage]
	#[pallet::unbounded]
	#[pallet::getter(fn message_leaves)]
	pub(super) type MessageLeaves<T: Config> = StorageValue<_, Vec<H256>, ValueQuery>;

	/// The current nonce for each message origin
	#[pallet::storage]
	pub type Nonce<T: Config> = StorageMap<_, Twox64Concat, ChannelId, u64, ValueQuery>;

```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L247-257)
```rust
		fn on_initialize(_: BlockNumberFor<T>) -> Weight {
			// Remove storage from previous block
			Messages::<T>::kill();
			MessageLeaves::<T>::kill();
			// Reserve some weight for the `on_finalize` handler
			T::WeightInfo::commit()
		}

		fn on_finalize(_: BlockNumberFor<T>) {
			Self::commit();
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-331)
```rust
		/// Process a message delivered by the MessageQueue pallet
		pub(crate) fn do_process_message(
			_: ProcessMessageOriginOf<T>,
			mut message: &[u8],
		) -> Result<bool, ProcessMessageError> {
			use ProcessMessageError::*;

			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);

			// Decode bytes into versioned message
			let versioned_queued_message: VersionedQueuedMessage =
				VersionedQueuedMessage::decode(&mut message).map_err(|_| Corrupt)?;

			// Convert versioned message into latest supported message version
			let queued_message: QueuedMessage =
				versioned_queued_message.try_into().map_err(|_| Unsupported)?;

			// Obtain next nonce
			let nonce = <Nonce<T>>::try_mutate(
				queued_message.channel_id,
				|nonce| -> Result<u64, ProcessMessageError> {
					*nonce = nonce.checked_add(1).ok_or(Unsupported)?;
					Ok(*nonce)
				},
			)?;

```

**File:** substrate/frame/message-queue/src/lib.rs (L1241-1252)
```rust
		while book_state.end > book_state.begin {
			let (processed, status) =
				Self::service_page(&origin, &mut book_state, weight, overweight_limit);
			total_processed.saturating_accrue(processed);
			match status {
				// Store the page progress and do not go to the next one.
				Bailed | NoProgress => break,
				// Go to the next page if this one is at the end.
				NoMore => (),
			};
			book_state.begin.saturating_inc();
		}
```

**File:** substrate/frame/message-queue/src/lib.rs (L1589-1608)
```rust
		match transaction {
			Err(Overweight(w)) if w.any_gt(overweight_limit) => {
				// Permanently overweight.
				Self::deposit_event(Event::<T>::OverweightEnqueued {
					id,
					origin,
					page_index,
					message_index,
				});
				MessageExecutionStatus::Overweight
			},
			Err(Overweight(_)) => {
				// Temporarily overweight - save progress and stop processing this
				// queue.
				MessageExecutionStatus::InsufficientWeight
			},
			Err(Yield) => {
				// Processing should be reattempted later.
				MessageExecutionStatus::Unprocessable { permanent: false }
			},
```
