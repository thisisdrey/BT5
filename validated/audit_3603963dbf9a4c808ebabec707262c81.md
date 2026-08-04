## Analysis

The core broken invariant in the external report is: **two operations that should be economically 1:1 linked are each priced independently using "current" market/valuation data captured at different points in time**, so a change in that data between the two operations breaks the 1:1 relationship, leaving one side unbacked or under/overpaid.

The closest local analog is Snowbridge's outbound queue (V1) fee/reward accounting, which reads the *same* mutable `PricingParameters` storage value twice, at two different pipeline stages that can be separated by an arbitrary number of blocks.

### Title
Snowbridge outbound queue charges the user fee and commits the relayer reward using `PricingParameters` snapshotted at two different times, decoupling collected fee from promised reward - (File: bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs, bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`SendMessage::validate` charges the local (DOT) fee to the message sender using `T::PricingParameters::get()` at **submission time**. The message is then handed to `T::MessageQueue`, which may only actually process it (via `do_process_message`) several blocks later. At that later point, `do_process_message` calls `T::PricingParameters::get()` **again**, independently, to compute the `reward` and `max_fee_per_gas` that are embedded into the `CommittedMessage` and ultimately paid out to the relayer on Ethereum. Because `PricingParameters` is a governance-mutable value that can be updated (via the `SetPricingParameters` command / `EthereumSystem`) at any time, the two reads can diverge, exactly mirroring the H-06 pattern of minting/valuing two sides of a bridge at "the current rate" at two different points in time.

### Finding Description
1. When a message is submitted (`SendMessage::validate` in `send_message_impl.rs`), the fee charged to the user is computed via: [1](#0-0) 
   This fee (in DOT) is deducted from the caller immediately by the XCM executor's `take_fee` right after `validate_export`/`validate`, i.e. at submission-time pricing.

2. The message is only *enqueued* at this point (`deliver()` → `T::MessageQueue::enqueue_message`), and actually processed asynchronously by `do_process_message`, which can run in a later block once the message queue's weight budget allows it: [2](#0-1) 

3. At that later processing time, `do_process_message` fetches `PricingParameters` **again**, fresh, and uses it to compute the `reward` and `max_fee_per_gas` fields that are baked into the `CommittedMessage` — the values that actually determine what a relayer is paid on Ethereum for delivering the message: [3](#0-2) 

There is no mechanism that snapshots or locks the pricing parameters at submission time and carries them through to processing/commit time. `PricingParameters` is a plain runtime storage value that governance can update (`SetPricingParameters` in `bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs`, lines 91-99) at any time between step 1 and step 3.

This is structurally the same bug class as H-06: the fee actually collected from the user (backing) is fixed at time T0 using rate R0, while the reward/gas commitment promised to a third party (relayer) is computed independently at time T1 (T1 > T0, unbounded delay determined by `MessageQueue` throughput) using rate R1. If R1 ≠ R0, the amount collected no longer matches the amount promised — just like `xezETH` minted on L2 at one valuation not matching `ezETH` minted on L1 at a later, different valuation.

### Impact Explanation
- If pricing parameters are updated to a **higher** `exchange_rate`/`fee_per_gas`/`rewards.remote` after messages are already queued but before they are processed, those already-fee-charged messages will commit a reward/gas budget that was never actually collected from their senders — the bridge is now promising ETH-side payouts on Ethereum that are not backed 1:1 by DOT fees collected on Polkadot for those specific messages. Over time and volume this creates an under-collateralized reward pool, mirroring the "bad debt" characterization given by the H-06 judge.
- If pricing parameters are updated **lower** after submission, already-queued (and already-paid-for) messages will commit a lower reward than what senders paid for, and/or a reward too small to economically justify off-chain relaying, which can cause queued messages to sit un-relayed — directly matching the "public underpriced work that ... stalls bridge processing" impact category.
- No malicious relayer, validator, or governance abuse is required — this is purely a timing/consistency gap between two independent reads of the same mutable pricing state during ordinary bridge operation and ordinary parameter updates.

### Likelihood Explanation
`PricingParameters` is expected to be updated periodically to track real ETH gas costs and the ETH/DOT exchange rate (this is exactly why `SetPricingParameters` exists as a governance command). The `MessageQueue` pallet is explicitly designed to defer processing across blocks when budgets are exceeded (see the `Yield` check in `do_process_message`), so a nonzero delay between `validate` (fee charge) and `do_process_message` (reward commitment) is a normal, expected occurrence, not an edge case. Any pricing update issued during that window — which will happen periodically over the bridge's lifetime — triggers the mismatch.

### Recommendation
Snapshot the `PricingParameters` (or at minimum, the derived `reward` and `max_fee_per_gas`) at `validate()`/fee-charging time inside the `Ticket`/`QueuedMessage`, and carry that snapshot through to `do_process_message`, instead of re-reading the live `PricingParameters` storage at commit time. This guarantees the committed reward is always backed by the fee actually collected for that specific message, closing the time-of-charge vs time-of-commit gap.

### Proof of Concept
1. Governance sets `PricingParameters` with `exchange_rate = R0`, `rewards.remote = r0`.
2. User submits a message; `validate()` computes and charges a DOT fee sized for `R0`/`r0` (`send_message_impl.rs` lines 59-60); message is enqueued into `MessageQueue`.
3. Before the `MessageQueue` schedules processing for this message (e.g. because the per-block message budget from `T::MaxMessagesPerBlock` was reached, hitting the `Yield` branch in `do_process_message`), governance calls the `SetPricingParameters` command, changing `exchange_rate`/`rewards.remote` to `R1`/`r1`.
4. `do_process_message` later runs for the deferred message and reads the *new* `PricingParameters` (`lib.rs` lines 332-352), embedding `reward = r1` (not `r0`) into the `CommittedMessage` that is relayed to Ethereum.
5. The relayer collects `r1` on Ethereum, but the protocol only collected fees from the user calibrated to `r0`. Repeated across many in-flight messages during a pricing update, this produces a systemic mismatch between fees collected and rewards promised, either under-collateralizing the bridge's reward payouts or leaving messages priced too low to attract relaying.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-313)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L332-352)
```rust
			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};
```
