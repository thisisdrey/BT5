### Title
Outbound queue commits a relayer reward computed from pricing parameters that can diverge from the fee actually charged to the sender - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`snowbridge_pallet_outbound_queue` computes the fee charged to a message sender in `SendMessage::validate` using the pricing parameters read *at send time*, but the `reward`/`max_fee_per_gas` values actually committed to Ethereum (and later paid out to relayers) are recomputed independently from `T::PricingParameters::get()` *at message-processing time* in `do_process_message`. The two reads of `PricingParameters` are not tied together, so if the parameters change between enqueue and processing, the amount collected from the sender and the amount promised to the relayer diverge — the same broken invariant described in the external report ("service fee can change between request and fulfillment").

### Finding Description
When a message is sent through the bridge, `Pallet::validate` computes the upfront `Fee` charged to the caller: [1](#0-0) 

using `T::PricingParameters::get()` at the moment of sending. This `Fee` is what the XCM executor/router actually withdraws from the sender's holding register when the extrinsic executes.

The message itself is enqueued into `T::MessageQueue` carrying only the `channel_id`/`command`, not the fee or pricing parameters that were used to charge the sender: [2](#0-1) 

The message queue processes messages asynchronously (subject to weight limits, congestion, and `MaxMessagesPerBlock`), so `do_process_message` may run in a later block than the one in which the sender was charged: [3](#0-2) 

At that (potentially later) point, `do_process_message` re-reads `T::PricingParameters::get()` fresh and uses it to compute the `reward` and `max_fee_per_gas` that are committed into the message that Ethereum's Gateway contract will later pay out to the relayer: [4](#0-3) 

`PricingParameters` is a normal governance-tunable value (`snowbridge-pallet-system::set_pricing_parameters`), and the module docs explicitly describe this as routine, expected behavior — "governance should manually update these parameters every few weeks": [5](#0-4) 

Because `calculate_fee` (used at send-time to charge the user) and the reward/fee-per-gas computation in `do_process_message` (used at commit-time to promise the relayer) both independently call `T::PricingParameters::get()` without caching the value used for the specific message, there is no guarantee the two computations use the same parameters. Nothing in `validate`, `deliver`, `QueuedMessage`, or `do_process_message` pins the pricing snapshot to the message — unlike the `PendingOrder`/tip mechanism in `outbound-queue-v2`, which does explicitly store the `fee` at enqueue time and resolves the reward from that stored value: [6](#0-5) 

The V1 outbound queue analyzed here has no equivalent caching, so it retains exactly the class of bug described in the external report.

### Impact Explanation
If pricing parameters increase (e.g., `rewards.remote` or `fee_per_gas` raised via a routine parameter update) between when a batch of messages was sent and when they are actually processed out of the message queue (which can span multiple blocks under load or during a backlog), the committed message promises a larger ETH reward/gas allowance to relayers than the fee that was actually collected from the original sender in that earlier block. This creates a shortfall between fees collected on the Polkadot side and the reward obligation committed to Ethereum, an unbacked-promise scenario for bridge economics. Conversely, if parameters decrease, senders are systematically overcharged relative to what is ultimately promised to relayers, with no refund mechanism, and relayers may find the committed reward too small to be profitable, causing genuine delivery messages to go unrelayed and stalling bridge processing.

### Likelihood Explanation
Parameter changes are explicitly documented as routine, expected operational events ("every few weeks"), not adversarial governance abuse — so the mismatch window is a normal, foreseeable operating condition rather than a contrived edge case. Any backlog in the `MessageQueue` (congestion, `MaxMessagesPerBlock` limits, or weight throttling) widens the time window during which an in-flight message can straddle a parameter update, making the divergence realistically reachable without any privileged or malicious actor initiating it maliciously.

### Recommendation
Snapshot the pricing parameters (or the fully computed `Fee`) at `validate`/`deliver` time and store them alongside the enqueued `QueuedMessage` (mirroring the `PendingOrder.fee` pattern already used in `outbound-queue-v2`). `do_process_message` should then use this cached value when constructing `reward` and `max_fee_per_gas` for the `CommittedMessage`, instead of re-reading `T::PricingParameters::get()` at commit time.

### Proof of Concept
1. Governance sets `PricingParameters` to `P1` (e.g., low `rewards.remote`).
2. A user sends a bridge message; `SendMessage::validate` computes and charges `Fee` based on `P1` (`send_message_impl.rs:59-61`); the message is enqueued into `MessageQueue` (`send_message_impl.rs:76-88`).
3. Before `do_process_message` runs for this message (e.g., queue backlog, or the update lands in the next block), governance calls `set_pricing_parameters` to `P2` with a higher `rewards.remote`/`fee_per_gas`.
4. `do_process_message` executes, reading the now-current `P2` and committing `reward`/`max_fee_per_gas` computed from `P2` into the `CommittedMessage` (`lib.rs:332-352`), even though the sender was only charged based on `P1`.
5. The committed message on-chain promises Ethereum-side relayers a reward larger than what was actually collected from the sender, reproducing the exact "fulfilled at a different fee than requested" mismatch from the external report.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-61)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L62-88)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L48-58)
```rust
//!
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L300-330)
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-443)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });

			Ok(true)
		}
```
