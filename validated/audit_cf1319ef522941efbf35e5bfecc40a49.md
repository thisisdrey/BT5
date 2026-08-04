## Title
Outbound queue fee charged at enqueue time diverges from reward/fee committed to Ethereum at dequeue time due to non-atomic re-read of `PricingParameters` - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs` / `bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs`)

### Summary
The Snowbridge outbound-queue pallet charges a user an upfront delivery fee computed from `T::PricingParameters::get()` at the moment the XCM message is validated (enqueue time), but the actual `reward` and `max_fee_per_gas` values committed into the message that is ultimately verified and paid out on Ethereum are computed by re-reading `T::PricingParameters::get()` a second, independent time when the message is dequeued and processed by `do_process_message`. Because these two reads are not atomic with respect to a single message and `PricingParameters` is a mutable, periodically-updated value (the module docs state governance updates it "every few weeks"), any message that sits in the `MessageQueue` across a parameter update will have its user-charged fee computed from one rate and its committed relayer reward/gas-fee computed from a different rate. This is the structural analog of the stETH/ETH oracle staleness bug: a value used to determine what a user pays diverges from the value used to determine what is actually settled, and the divergence is baked into the protocol logic rather than requiring a malicious actor.

### Finding Description
`SendMessage::validate` on `Pallet<T>` (`bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs`, lines 41-74) computes the fee the user is charged: [1](#0-0) 
This fee, derived from `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())`, is converted into an XCM `Asset` and charged to the sender via `BuyExecution`/`PayFees` (`bridges/snowbridge/primitives/outbound-queue/src/v1/converter/mod.rs`, lines 129-137).

The queued message is then only enqueued into `T::MessageQueue` (`fn deliver`) and is processed later, potentially many blocks afterward, by `Pallet::do_process_message`: [2](#0-1) 
Note that `pricing_params` is re-fetched fresh at line 332 (`let pricing_params = T::PricingParameters::get();`), and this second, independent reading of `PricingParameters` is what determines `max_fee_per_gas` and `reward` written into the `CommittedMessage` that is merkleized, committed to the parachain header, and ultimately verified/paid out on the Ethereum Gateway contract.

`calculate_fee` itself uses `PricingParameters` a third time, again independently, purely for the up-front user charge: [3](#0-2) 

The module documentation confirms that `PricingParameters` (containing `exchange_rate`, `fee_per_gas`, `multiplier`, `rewards`) is not a live oracle but a manually governed value updated only periodically: [4](#0-3) 

There is no mechanism binding the pricing snapshot used at `validate`/enqueue time to the pricing snapshot used at `do_process_message`/commit time for the *same* message — the two reads of the same storage item, separated by an arbitrary number of blocks while the message sits in `MessageQueue`, are treated as if they were consistent. The queue itself can legitimately hold messages across many blocks: `do_process_message` self-throttles via `Yield` when `MessageLeaves::decode_len() >= T::MaxMessagesPerBlock::get()`: [5](#0-4) 
so under normal (non-adversarial) load a governance-driven pricing update landing between a user's `validate` call and that message's eventual `do_process_message` call is a realistic, unprivileged-attacker-independent event.

### Impact Explanation
If `PricingParameters` increases between enqueue and dequeue (e.g., ETH gas price or the ETH/DOT exchange rate rises, or `rewards.remote`/`fee_per_gas` is raised by governance), the `CommittedMessage` sent to Ethereum promises a `reward` and `max_fee_per_gas` higher than what was actually collected from the user at enqueue time. Since relayer reward accrual on the Ethereum Gateway is `Min(GasPrice, Message.MaxFeePerGas) * GasUsed() + Message.Reward` (per the module docs), the protocol is now committed to paying relayers more than the fee actually collected — the shortfall is a value-conservation break funded implicitly out of BridgeHub's sovereign/treasury balance, i.e., an unbacked payout. Conversely, if parameters fall between enqueue and dequeue, users are overcharged for a commitment that ends up cheaper, and the surplus is not returned. Either direction breaks the "settle exactly once to the rightful beneficiary and amount" invariant for bridge reward payouts, and in the underpriced direction can also make queued messages economically unprofitable for relayers to service, stalling bridge message processing — both impacts explicitly listed as in-scope.

### Likelihood Explanation
This does not require a malicious peer, relayer, governance actor, or leaked key. It only requires the routine, expected `set_pricing_parameters` governance maintenance mentioned in the pallet's own documentation to occur while any message is sitting in `MessageQueue` awaiting processing — an ordinary, foreseeable operational sequence given the pallet explicitly throttles processing via `Yield` and queue backlogs are a normal condition under load. No attacker action is needed to trigger the mismatch; it is a latent correctness bug in the fee/commit pipeline.

### Recommendation
Snapshot the pricing parameters once at `validate`/enqueue time and carry that snapshot through the `Ticket`/`QueuedMessage` so that the exact same values used to compute the user's charged fee are the ones written into the `CommittedMessage` at `do_process_message` time, rather than re-reading `T::PricingParameters::get()` independently at commit time. Alternatively, if a fresh read at commit time is required for accuracy, cap the committed `reward`/`max_fee_per_gas` to what the collected fee can cover, and refund/reconcile any surplus, ensuring the amount promised to Ethereum can never exceed the amount actually escrowed from the sender.

### Proof of Concept
1. Governance sets `PricingParameters` (`exchange_rate = R1`, `fee_per_gas = G1`, `rewards.remote = W1`).
2. A user submits an XCM that routes through `EthereumBlobExporter::validate`, which calls `OutboundQueue::validate`; the message is charged fee `F1 = calculate_fee(gas, {R1, G1, W1, multiplier})` and the message is enqueued into `MessageQueue` but not yet processed (e.g., the queue is near `MaxMessagesPerBlock` or simply not yet visited in this block).
3. Governance calls `set_pricing_parameters` to update to `{R2, G2, W2}` (a routine, expected maintenance action per the module docs), before the enqueued message is dequeued.
4. `MessageQueue` later invokes `do_process_message` for the pending message; `pricing_params = T::PricingParameters::get()` now returns `{R2, G2, W2}`, so `max_fee_per_gas` and `reward` in the resulting `CommittedMessage` are computed from the new parameters, not `F1`'s parameters.
5. The relayer, upon delivering this message to the Ethereum Gateway, can claim `Min(GasPrice, max_fee_per_gas_from_R2G2) * GasUsed + reward_from_W2`, which can exceed the fee `F1` actually collected from the user at step 2 — demonstrating the value mismatch between what was charged and what is paid out for the identical message.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
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
```
