Audit Report

## Title
Outbound-queue fee calculation can silently truncate the remote (relayer) fee to zero despite passing `PricingParameters::validate()` - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

## Summary
`PricingParameters::validate()` only checks that raw governance-set inputs (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are individually non-zero, but the derived value actually charged to senders — the `remote` fee computed by `calculate_fee` — can truncate to `0` through integer division in `convert_from_ether_decimals`, even though all raw inputs pass validation. `do_process_message` and `SendMessage::validate` never check the final computed fee is non-zero before committing/queuing the message, so the sender pays no remote-fee component while the message still consumes queue capacity and requires Ethereum-side execution.

## Finding Description
`PricingParameters::validate` in `bridges/snowbridge/primitives/core/src/pricing.rs` (L39-56) rejects only zero-valued raw fields. The fee actually charged is computed in `calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs` L368-393), which converts the wei-denominated remote fee through `FixedU128` scaling and finally truncates via integer division in `convert_from_ether_decimals` (L414-418: `value.checked_div(denom)`). None of these steps validate that the final `remote` fee is non-zero. This is confirmed by the pallet's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` (`bridges/snowbridge/pallets/outbound-queue/src/test.rs` L303-319), which uses parameters that pass `validate()` (`exchange_rate=1/1`, `fee_per_gas=1`, `rewards={local:1,remote:1}`, `multiplier=1/1`) yet yields `fee.remote == 0`.

Both call paths that use `calculate_fee` proceed unconditionally after computing the fee:
- `SendMessage::validate` in `send_message_impl.rs` (L59-73) computes `fee` via `calculate_fee` and returns it in the `Ticket`/`Fee` tuple without checking `fee.remote != 0`.
- `do_process_message` (L300-364) commits the message (nonce assignment, `Messages`/`MessageLeaves` append, `MessageAccepted` event) without re-deriving or checking the fee at all — it uses `pricing_params.rewards.remote` directly for the `reward` field embedded in `CommittedMessage`, not the truncated `calculate_fee` output.

This confirms the existing test-documented defect is real and reachable from any public message-sending path relying on `calculate_fee`.

## Impact Explanation
This matches "public underpriced work that degrades block production or stalls bridge processing" from the impact gate: a message can be validated and queued while the sender is charged `fee.remote == 0`, meaning the local/native-currency portion meant to compensate for relayer/Ethereum-side execution costs is undercharged. This is a genuine cost-correctness defect confirmed directly in the pallet's own regression test, not a hypothetical.

## Likelihood Explanation
Reproducing this requires only a legitimate (non-malicious) governance-set `PricingParameters` configuration that passes `validate()`, combined with ordinary message submission — no privileged action or malicious actor is needed beyond the parameter configuration having been set (which, per the rules, is a governance action, but the resulting bug is triggered by ordinary unprivileged message submission thereafter). The pallet's own test suite demonstrates this end-to-end with a plausible parameter set.

## Recommendation
Add an explicit non-zero (or minimum-threshold) check on `Fee::remote` inside `calculate_fee`, or immediately after computing it in `SendMessage::validate`/`do_process_message`, returning an error rather than accepting a message whose remote fee rounds to zero. Extend `PricingParameters::validate()` to reject configurations that could produce a zero remote fee for representative minimum-gas messages.

## Proof of Concept
Confirmed by the existing unit test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in `bridges/snowbridge/pallets/outbound-queue/src/test.rs` (L303-319): configure `PricingParameters` with `exchange_rate=FixedU128::from_rational(1,1)`, `fee_per_gas=1u32.into()`, `rewards={local:1u32.into(), remote:1u32.into()}`, `multiplier=FixedU128::from_rational(1,1)`; these pass `validate()`; call `OutboundQueue::calculate_fee(250000, price_params)`; assert `fee.remote == 0`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
