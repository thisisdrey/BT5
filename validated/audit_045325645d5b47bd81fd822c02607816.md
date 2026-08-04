### Title
Outbound-queue fee calculation can silently truncate the remote (relayer) fee to zero despite passing `PricingParameters::validate()` - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
`PythOffchainLookupNode.process` accepted an externally supplied price without checking that its *derived* quality metric (confidence interval) was within a trustworthy bound, even though the raw inputs looked individually valid. The Snowbridge outbound-queue pallet has the same class of bug: `PricingParameters::validate()` only checks that the raw governance-set inputs (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are individually non-zero, but never validates the *derived* value that is actually used downstream — the computed `remote` fee that is supposed to fund/incentivize the Ethereum-side relayer. That derived value can truncate to `0` through integer division/decimals conversion even when every input passes `validate()`, and the pallet accepts and commits the message anyway.

### Finding Description
`PricingParameters::validate` only rejects zero-valued raw fields: [1](#0-0) 

The actual fee that a sender pays (and that funds the relayer reward on Ethereum) is computed in `calculate_fee`, which converts a `U256` wei-denominated remote fee to `u128`, then into a `FixedU128` scaled value, multiplies by `multiplier`, divides by `exchange_rate`, and finally truncates via integer division in `convert_from_ether_decimals`: [2](#0-1) 

None of these steps check whether the final `remote` fee value is non-zero before it is used to construct the `Fee` returned to the caller (and ultimately charged/settled). The pallet's own test suite documents this exact defect: [3](#0-2) 

The test uses `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}` — all individually non-zero and passing `PricingParameters::validate()` — yet `calculate_fee` returns `fee.remote == 0`, with a comment stating "the remote fee calculated here is invalid which should be avoided." The same truncation-to-zero can occur for any legitimately configured (non-degenerate) parameter set whenever `gas_used_at_most * fee_per_gas + reward` (in wei) is small relative to `ETHER_DECIMALS - T::Decimals` (10^8 for a 10-decimal chain, 10^6 for 12-decimal), which is entirely plausible for lightweight commands with small `max_dispatch_gas`.

This mirrors the Pyth bug's core broken invariant: the code validates the *presence/non-zeroness* of raw inputs but never validates the *quality/soundness of the derived output value* that downstream logic relies on to be economically meaningful, so a value that superficially "passes validation" can still be a degenerate/untrustworthy result once used.

### Impact Explanation
`do_process_message` commits the message (assigns nonce, appends to `Messages`/`MessageLeaves`, emits `MessageAccepted`) unconditionally once decoding succeeds — it does not re-check that the fee computed for the message is non-zero: [4](#0-3) 

If the remote fee component silently truncates to zero for a class of messages, the `reward` field embedded in the committed message (`reward.try_into().defensive_unwrap_or(u128::MAX)`) is derived from `pricing_params.rewards.remote` directly (not the truncated fee), so the Ethereum-side relayer reward itself is not affected by this particular truncation; however, the *local currency* fee actually deducted from the sender (used to fund that Ethereum-side reward, per the module's own fee-computation documentation) is what truncates to zero. This means the chain accepts and commits "public underpriced work": the sender is not charged the intended remote-fee/reward-funding compensation, while the message still consumes queue capacity, weight, storage, and eventually requires costly relayer/Ethereum gas execution. Under sustained triggering (e.g., a governance-set-but-plausible pricing configuration or lightweight command types), this degrades the fee model's ability to fund relayer incentives and can starve/stall bridge message processing on the Ethereum side, aligning with the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
Likelihood is low-to-moderate: it requires a governance-set `PricingParameters` combination (which does pass the existing `validate()` checks) together with a message whose `max_dispatch_gas` is small enough that `fee_per_gas * gas_used_at_most + reward` (in wei), after conversion to the chain's native decimals, rounds down to zero. This does not require a malicious peer, validator, or governance actor doing anything malicious — only a legitimate, easy-to-construct parameter set (as the pallet's own regression test demonstrates) combined with ordinary message submission by any user.

### Recommendation
Add an explicit non-zero (or minimum-threshold) check on the final computed `Fee::remote` value inside `calculate_fee` (or immediately after computing it in `do_process_message`), returning an error (e.g. a new `Error::<T>::InvalidFee`) rather than silently committing a message whose remote fee rounds to zero. Additionally, extend `PricingParameters::validate()` (or add a companion invariant check exercised at parameter-update time and at message-commit time) to reject configurations for which representative/minimum-gas messages would produce a zero remote fee after the full computation pipeline, not just non-zero raw inputs.

### Proof of Concept
The existing unit test already demonstrates the defect end-to-end: [3](#0-2) 
1. Configure `PricingParameters { exchange_rate: FixedU128::from_rational(1,1), fee_per_gas: 1u32.into(), rewards: Rewards { local: 1u32.into(), remote: 1u32.into() }, multiplier: FixedU128::from_rational(1,1) }`.
2. Call `PricingParameters::validate()` — it returns `Ok(())` since all fields are non-zero.
3. Call `OutboundQueue::calculate_fee(gas_used, price_params)` with `gas_used = 250000` — the returned `Fee.remote` is `0`, confirmed by the assertion `assert_eq!(fee.remote, 0)`.
4. Submitting a message under such a parameter configuration via `do_process_message` proceeds to commit the message without ever checking `fee.remote != 0`, i.e., the message is queued/committed for delivery to Ethereum while the sender pays no remote-fee/relayer-funding component.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-418)
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

		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-319)
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
}
```
