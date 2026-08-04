## Finding

The Snowbridge outbound queue treats governance-set `PricingParameters` (the ETH/DOT "exchange rate" oracle plus `fee_per_gas`/`reward`) as a value that can be read independently at two different points in the message lifecycle, without binding a snapshot to the message itself. This is a direct structural analog of the report's "TWAP of TWAPs" / stale-oracle mismatch: the same externally-set price is consulted twice, at different times, and nothing forces the two readings to agree.

### Title
Fee charged at message enqueue and reward promised at message commit are computed from independently-read `PricingParameters`, allowing settlement mismatch - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
When a message is submitted to the Snowbridge outbound queue, the delivery fee charged to the sender is computed in `SendMessage::validate` using `T::PricingParameters::get()` at that block. The message is then queued and later dequeued by `pallet_message_queue`, which invokes `do_process_message` potentially many blocks later; at that point the pallet re-reads `T::PricingParameters::get()` a second time to populate `max_fee_per_gas` and `reward` in the `CommittedMessage` that is ABI-encoded and delivered to the Ethereum Gateway contract. The `QueuedMessage` struct that sits in the queue between these two reads carries no snapshot of the pricing used to charge the fee. [1](#0-0) [2](#0-1) 

### Finding Description
`SendMessage::validate` computes the user-facing fee via `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`, using the exchange rate/fee-per-gas/reward that is live at enqueue time. [3](#0-2) 

That fee is what the caller (e.g. `pallet_xcm`/`EthereumBlobExporter`, or `pallet_system::send`) actually collects. But the ticket/`QueuedMessage` only carries `{id, channel_id, command}` — no pricing snapshot — before being enqueued into `T::MessageQueue`. [4](#0-3) 

Later, when the message queue actually processes the message (which can be an arbitrary number of blocks later, since `MessageQueue` throttles per-block processing and `do_process_message` itself yields once `MaxMessagesPerBlock` is hit), `do_process_message` fetches `T::PricingParameters::get()` again and bakes the *current* `fee_per_gas` and `rewards.remote` into the `CommittedMessage` that is what relayers/the Ethereum Gateway contract will actually use to pay out: [5](#0-4) [2](#0-1) 

The pallet's own documentation confirms that governance is expected to routinely update these parameters ("every few weeks"), and the system pallet's `set_pricing_parameters` extrinsic can change `exchange_rate`, `fee_per_gas`, and `multiplier` at any time with no coordination with in-flight queued messages: [6](#0-5) [7](#0-6) 

Consequently, for any message that straddles a `set_pricing_parameters` update between its `validate` (fee charge) and its `do_process_message` (reward/fee commitment), the amount collected from the sender and the amount promised to the relayer/Gateway diverge. Nothing in the pipeline re-validates or reconciles these two amounts — `do_process_message` unconditionally re-derives `max_fee_per_gas`/`reward` from current storage and commits it, regardless of what was actually charged when the ticket was created.

### Impact Explanation
- If parameters increase (e.g. rising ETH gas price, higher reward) between enqueue and commit, the committed message promises Ethereum-side payouts larger than what was collected from the user on the Polkadot side for that specific message — an unbacked/under-collected settlement that must be covered from the bridge's aggregate fee pool, silently transferring cost from the protocol/other users to cover this message's relayer reward.
- If parameters decrease between enqueue and commit, the relayer reward embedded in the committed message is now lower than what the user already paid for — reducing relayer incentive to promptly relay that specific message (since actual gas-refund/reward is now below what covers real Ethereum gas cost), which can stall delivery of that message and degrade bridge throughput, matching the "public underpriced work that … stalls bridge processing" impact class.
- Because `QueuedMessage`/`Ticket` never snapshot the pricing parameters used to charge the fee, there is no mechanism to detect or correct this divergence; it happens silently on every governance pricing update that occurs while messages are in flight, which per the module's own docs is an expected, recurring, non-privileged-attacker event.

### Likelihood Explanation
This does not require a malicious governance actor: it happens under the pallet's normal, documented operating procedure of periodically updating `PricingParameters`. Any window during which messages are queued (subject to `MaxMessagesPerBlock` throttling and general `MessageQueue` processing delay) while a legitimate, routine pricing update occurs will trigger the mismatch. No attacker action is required beyond normal usage timing; the flaw is in the pallet's failure to bind the charged fee to the committed message.

### Recommendation
Snapshot the `PricingParameters` (or at least the derived `max_fee_per_gas`/`reward`/exchange rate) into the `QueuedMessage`/`Ticket` at `validate` time, and have `do_process_message` use that snapshotted value when constructing the `CommittedMessage`, rather than re-reading current `PricingParameters` storage. This guarantees the fee actually collected from the sender is exactly what is promised/settled to relayers on Ethereum for that message, closing the gap between the two independent oracle-price reads.

### Proof of Concept
1. User calls into `EthereumBlobExporter`/`pallet_system::send`, which invokes `SendMessage::validate`; fee is computed and collected using `PricingParameters { exchange_rate: R1, fee_per_gas: G1, reward: W1, ... }`. [3](#0-2) 
2. Message is enqueued into `T::MessageQueue`; due to `MaxMessagesPerBlock` throttling (`Yield` in `do_process_message`) or general queue backlog, processing is deferred several blocks. [5](#0-4) 
3. Governance calls `set_pricing_parameters` with new `PricingParameters { exchange_rate: R2, fee_per_gas: G2, reward: W2 }` in between, as documented to be routine. [8](#0-7) 
4. `do_process_message` finally runs, re-reads `T::PricingParameters::get()` (now R2/G2/W2), and commits `max_fee_per_gas = G2`, `reward = W2` into the `CommittedMessage` sent to Ethereum — while the sender only paid based on R1/G1/W1. [2](#0-1) 
5. Result: settlement amount promised on Ethereum for this message no longer matches the amount actually collected from the user for it, with no reconciliation or error raised anywhere in the pipeline.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-73)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-58)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L307-313)
```rust
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

**File:** bridges/snowbridge/pallets/system/src/lib.rs (L310-333)
```rust
		/// Set pricing parameters on both sides of the bridge
		///
		/// Fee required: No
		///
		/// - `origin`: Must be root
		#[pallet::call_index(2)]
		#[pallet::weight((T::WeightInfo::set_pricing_parameters(), DispatchClass::Operational))]
		pub fn set_pricing_parameters(
			origin: OriginFor<T>,
			params: PricingParametersOf<T>,
		) -> DispatchResult {
			ensure_root(origin)?;
			params.validate().map_err(|_| Error::<T>::InvalidPricingParameters)?;
			PricingParameters::<T>::put(params.clone());

			let command = Command::SetPricingParameters {
				exchange_rate: params.exchange_rate.into(),
				delivery_cost: T::InboundDeliveryCost::get().saturated_into::<u128>(),
				multiplier: params.multiplier.into(),
			};
			Self::send(PRIMARY_GOVERNANCE_CHANNEL, command, PaysFee::<T>::No)?;

			Self::deposit_event(Event::PricingParametersChanged { params });
			Ok(())
```
