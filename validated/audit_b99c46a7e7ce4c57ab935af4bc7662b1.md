### Title
Snowbridge outbound-queue commits a stale relayer `reward` at processing time that can diverge from the fee actually collected from the user at submission time - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Perennial bug involved a liquidation that used one price source (`oracle.latest()`, which could be zero/stale) while the rest of the `Market` logic used a different, valid price — the two paths diverged and could later revert or misbehave. The Snowbridge outbound-queue pallet has the same class of bug: the fee actually charged to (and paid by) the user when a message is submitted is computed from `T::PricingParameters::get()` **at submission time**, but the relayer `reward` that is ultimately embedded into the committed message (and paid out on Ethereum) is fetched from `T::PricingParameters::get()` **again, independently, at processing time** — which can be a different block, with different governance-updated pricing parameters.

### Finding Description
When a user (or another pallet) sends a message via `Pallet::<T>::validate`, the fee charged is computed once, using the pricing parameters that are live at that moment: [1](#0-0) 

This fee is what is deducted from the user (via the caller's XCM/fee-charging logic) and the message is then merely enqueued into `T::MessageQueue` as an opaque `Ticket`: [2](#0-1) 

The message sits in the message queue until it is later serviced (subject to `MaxMessagesPerBlock` and available weight) by `do_process_message`. At that point, the pallet re-reads `T::PricingParameters::get()` **again** — not the value used when the fee was originally charged — and uses `pricing_params.rewards.remote` to build the `reward` field of the `CommittedMessage` that is ultimately sent to the Ethereum gateway and paid to relayers: [3](#0-2) 

The fee-calculation function itself independently derives the same `params.rewards.remote` field when computing the amount to charge the user: [4](#0-3) 

Because `T::PricingParameters` is a governance-updatable value (updated periodically, "every few weeks" per the module docs) and messages can remain queued across many blocks before being serviced (bounded only by `MaxMessagesPerBlock` and weight availability), there is a real window in which:
1. A user submits a message and pays a fee based on pricing parameters `P1` (specifically `P1.rewards.remote`, `P1.fee_per_gas`, `P1.exchange_rate`, `P1.multiplier`).
2. Before the message is dequeued and processed, governance calls `set_pricing_parameters` to update to `P2`.
3. `do_process_message` runs with `P2` and embeds `P2.rewards.remote` (and `P2.fee_per_gas` as `max_fee_per_gas`) into the `CommittedMessage` that is delivered to Ethereum.
4. The relayer who delivers the message to Ethereum is refunded based on `P2` values embedded in the message, not `P1` values that the user actually paid for.

This exactly mirrors the Perennial pattern: two code paths (fee charged vs. reward/params committed) that are supposed to represent the same economic value use two independently-fetched snapshots of a mutable price/parameter source, with no binding or snapshotting of the value used at submission time into the ticket/queued message itself. The module's own documented invariant — `Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)` where `RemoteFeeAdjusted` is derived from the same `Params.Reward` that is later committed — silently breaks whenever parameters change between submission and processing, since nothing snapshots `P1` into the queued ticket.

### Impact Explanation
If pricing parameters increase between submission and processing (e.g., ETH/DOT exchange rate moves against DOT, or governance raises `fee_per_gas`/`rewards.remote` to keep pace with gas costs), the bridge commits and pays out a **higher** reward/gas-refund on Ethereum than what was collected from the user on the Polkadot side. This is a value-conservation break: the bridge's Ethereum-side payout obligation is decoupled from the fee actually collected, meaning the protocol underprices delivery for messages that were queued before a parameter increase — this can drain the bridge's reward reserve faster than fees replenish it, a form of unbacked payout. This fits the "public underpriced work that degrades... stalls bridge processing" and "duplicate settlement or payout" impact categories in the gate: the committed obligation for a message is settled at a rate the payer never agreed to or paid for.

Conversely, if parameters decrease, relayers may be underpaid relative to what the user paid, which can cause relayers to stop servicing/delivering that message class, contributing to bridge processing stalls for the affected messages.

### Likelihood Explanation
Likelihood is low but non-zero, directly analogous to Sherlock's own characterization of the original Perennial issue ("very low but still possible"). It requires:
- The message queue to have a backlog (plausible under load, rate limits via `MaxMessagesPerBlock`, or governance halts/pauses).
- A pricing-parameter update to land while messages from the old parameter regime are still queued.

No malicious actor is required — this is a routine governance maintenance operation (documented as expected to happen "every few weeks") combined with normal queueing delay, not governance abuse.

### Recommendation
Snapshot the pricing parameters (or at minimum `rewards.remote` and `fee_per_gas`) into the `Ticket`/`QueuedMessage` at `validate()` time, and use that snapshotted value in `do_process_message` when constructing `CommittedMessage`, instead of re-fetching `T::PricingParameters::get()` at processing time. This guarantees the fee charged to the user and the reward/gas-refund actually committed to Ethereum are always derived from the same parameter snapshot.

### Proof of Concept
1. Set `PricingParameters = P1` with `rewards.remote = R1`.
2. Call `Pallet::<T>::validate(message)` — fee charged to caller reflects `R1` (via `calculate_fee`).
3. `deliver(ticket)` enqueues the message; do not let the queue service it yet (simulate backlog / insufficient weight).
4. Governance calls `set_pricing_parameters` to `P2` with `rewards.remote = R2 > R1`.
5. Let the message queue process the pending message via `do_process_message` — observe the resulting `CommittedMessage.reward == R2`, not `R1`.
6. Compare: the amount withdrawn from the user at step 2 (based on `R1`) is strictly less than the reward the bridge has now committed to pay a relayer on Ethereum (`R2`), demonstrating the fee/obligation mismatch.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

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
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-88)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-364)
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

			// ABI-encode and hash the prepared message
			let message_abi_encoded = ethabi::encode(&[message.clone().into()]);
			let message_abi_encoded_hash = <T as Config>::Hashing::hash(&message_abi_encoded);

			Messages::<T>::append(Box::new(message));
			MessageLeaves::<T>::append(message_abi_encoded_hash);

			Self::deposit_event(Event::MessageAccepted { id: queued_message.id, nonce });

			Ok(true)
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-402)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
		}

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}
```
