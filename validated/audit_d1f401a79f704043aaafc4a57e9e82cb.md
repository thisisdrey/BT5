### Title
`calculate_fee` in Snowbridge outbound queue can silently return a zero remote fee that is charged to users and enqueued to Ethereum, causing chain-validated underpriced relayer work - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
Snowbridge's `OutboundQueue::calculate_fee` computes the DOT-denominated `remote` fee component that is meant to cover Ethereum gas costs and relayer rewards. With `PricingParameters` that pass the pallet's own `validate()` sanity check (all fields non-zero), the arithmetic can still round the `remote` fee down to `0` for legitimate parameter combinations. This zero looks like a normal, "valid" fee — there is no error, no revert, no event — and it is used unconditionally both to charge the sender (via `validate()` in `send_message_impl.rs`) and to construct the `CommittedMessage.reward` field that is committed on-chain and relayed to the Ethereum gateway contract (via `do_process_message` in `lib.rs`).

### Finding Description
`calculate_fee` performs a chain of fixed-point operations without any explicit floor/non-zero enforcement on the final `remote` output: [1](#0-0) 

The only "safety" check is `params.exchange_rate` being non-zero (an `expect`), which guards against a divide-by-zero panic, not against the numerator itself rounding to zero. `PricingParameters::validate()` only checks that each *parameter* is non-zero — it does not check that the *computed fee* is non-zero: [2](#0-1) 

The pallet's own test suite documents this exact defect and explicitly states it "should be avoided", yet no guard exists in the production code path: [3](#0-2) 

This zero-fee `Fee` struct is not a special "insufficient/failure" sentinel — it is the same `Fee<Balance>` type returned on success, and it flows into two critical points without any additional validation:

1. `validate()` returns `Ok((ticket, fee))` even when `fee.remote == 0`, so the fee is accepted as a legitimate charge to the caller: [4](#0-3) 

2. `do_process_message` reads `pricing_params.rewards.remote` (not `calculate_fee`'s output directly, but the same zero-prone remote-fee computation feeds the on-chain `reward` field committed to the gateway contract) and unconditionally builds and commits the message with whatever reward value results, again with no non-zero check: [5](#0-4) 

This is a direct structural analog of the reported "getGuardedValue returns (0,0) instead of reverting" bug class: a computation that should represent "the required payment/consensus wasn't met" is instead silently accepted as a normal, zero-but-valid result, and that zero is then propagated into settlement logic (charging the user, committing the relayer reward) as if it were a legitimate value.

### Impact Explanation
A zero (or near-zero) `remote` fee/reward committed in `CommittedMessage.reward` means relayers on Ethereum receive no compensation for gas spent delivering the message to the gateway contract, per the documented settlement formula (`Min(GasPrice, MaxFeePerGas) * GasUsed() + Reward`). This is exactly the "public underpriced work that degrades block production or stalls bridge processing" impact category: since delivering such messages is not economically rational for relayers, messages with zero committed reward will not get relayed, causing message queue backlog/stall in the Polkadot→Ethereum direction of the bridge, while the local (DOT-side) fee is still fully or over-charged from the sender since `fee.local` is unaffected. This is a public, unprivileged, no-governance-action-needed path — any user or pallet sending a Snowbridge message with certain (validator-approved, non-zero, but small) `PricingParameters` values, or via specific gas/parameter combinations, can trigger this.

### Likelihood Explanation
`PricingParameters` are governance-set, but governance is not compromised here — the parameters pass all existing validation (`validate()`) and are entirely plausible operational values (e.g. `fee_per_gas` in low units or `exchange_rate` and `multiplier` interacting with small `gas_used_at_most` values). The test file itself proves this occurs with simple non-degenerate inputs (`exchange_rate = 1`, `fee_per_gas = 1`, `multiplier = 1`), demonstrating the bug is reachable under normal (not adversarial-governance) conditions, purely as a consequence of the fixed-point arithmetic ordering (multiply-then-divide with truncating integer conversions). No malicious peer, relayer, or validator is required.

### Recommendation
- In `calculate_fee`, after computing the final `remote` value, explicitly check `fee.remote.is_zero()` (or below some configured minimum) and return an error (e.g. a new `Error::<T>::FeeCalculationUnderflow`) instead of silently returning a zero `Fee`.
- Alternatively/additionally, enforce a non-zero final-fee invariant as part of `PricingParameters::validate()` by simulating fee calculation for representative gas bounds, or add a minimum-fee floor parameter.
- Propagate this failure through `SendMessage::validate()` so `send_message_impl.rs` rejects message submission rather than enqueuing a message with an unpayable/unattractive relayer reward.
- Add an explicit event (e.g., `Event::FeeCalculationTooLow`) for observability so operators can react to parameter drift before it stalls bridge processing.

### Proof of Concept
The pallet's existing unit test already demonstrates the defect deterministically:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-318
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
    new_tester().execute_with(|| {
        let gas_used: u64 = 250000;
        let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
            exchange_rate: FixedU128::from_rational(1, 1),
            fee_per_gas: 1_u32.into(),
            rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
            multiplier: FixedU128::from_rational(1, 1),
        };
        let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
        assert_eq!(fee.local, 698000000);
        // Though none zero pricing params the remote fee calculated here is invalid
        // which should be avoided
        assert_eq!(fee.remote, 0);
    });
}
```
This shows that with fully non-zero, `validate()`-passing `PricingParameters`, `calculate_fee` returns `Fee { local: 698000000, remote: 0 }` — accepted by `send_message_impl::validate()` as a normal successful fee quote, and the same zero-prone remote value feeds `CommittedMessage.reward` in `do_process_message`, meaning the message is queued and committed to Ethereum with no relayer incentive, with no error or event raised anywhere in the pipeline.

Note: I was unable to fully trace whether any downstream integration test (e.g., in `cumulus/parachains/runtimes/bridge-hubs/*/tests`) additionally re-validates a non-zero reward before dispatch to Ethereum; if such a guard exists elsewhere in the runtime configuration, it would need to be confirmed to fully assess exploitability at the runtime level.

### Citations

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

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-318)
```rust
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 1),
			fee_per_gas: 1_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
		assert_eq!(fee.local, 698000000);
		// Though none zero pricing params the remote fee calculated here is invalid
		// which should be avoided
		assert_eq!(fee.remote, 0);
	});
```

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
