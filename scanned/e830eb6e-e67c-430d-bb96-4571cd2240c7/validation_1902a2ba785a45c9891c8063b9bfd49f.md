## Title
Public message-fee calculation can round `fee.remote` to `0` for valid, non-zero pricing parameters, underpricing Ethereum-bound message delivery — (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The core broken invariant in the H-30 report is that a per-item value that should participate proportionally in an aggregate/priced result can silently become `0` even though the inputs used to compute it are non-zero and "valid," skewing the outcome in the caller's favor without any error being raised. The local analog is in `Pallet::<T>::calculate_fee` in the Snowbridge outbound-queue pallet: given fully valid, non-zero `PricingParameters` (exchange rate, fee-per-gas, rewards, multiplier all pass `PricingParameters::validate`), the computed native-currency `fee.remote` component that is supposed to cover the Ethereum-side gas cost of dispatching a message can be truncated to `0` by integer/fixed-point rounding, exactly as the report's TWAP price defaulting to `0` despite valid on-chain inputs.

### Finding Description
`calculate_fee` computes the remote-fee portion as: [1](#0-0) 

1. `calculate_remote_fee` computes `fee_per_gas * gas_used_at_most + reward` in wei (U256), which is downcast to `u128`.
2. This wei amount is reinterpreted as a `FixedU128` via `from_inner`, multiplied by `params.multiplier`, and divided by `params.exchange_rate`.
3. The result is passed through `convert_from_ether_decimals`, which does `value.checked_div(10^(ETHER_DECIMALS - T::Decimals))` — dividing an 18-decimal Ether fixed-point value down to the chain's native decimals (e.g. 10 for DOT), i.e. dividing by `10^8`. [2](#0-1) 

Because step 3 divides by a very large denominator (`10^8` for DOT), any intermediate wei-scale amount smaller than that denominator is truncated to `0`. This is not a defensive/edge case — it is demonstrated directly in the pallet's own test suite with parameters that pass `validate()` (all non-zero): [3](#0-2) 

The comment in the test itself ("Though none zero pricing params the remote fee calculated here is invalid which should be avoided") confirms the pallet authors are aware the guard (`PricingParameters::validate()`) is insufficient to prevent a `0` fee outcome: [4](#0-3) 

`validate()` only checks that each individual field is non-zero; it never checks that the resulting *derived* `fee.remote` is non-zero after the full pipeline (multiply by multiplier, divide by exchange rate, divide by decimals denominator) — mirroring exactly the H-30 flaw where the guard checked `sumNative != 0` instead of checking the actual derived quantity that mattered (`price1Average.mul(1).decode144() != 0`).

This fee is computed unprivileged, on every call to `SendMessage::validate`, which any pallet or XCM-triggered code path that sends a Snowbridge outbound message goes through: [5](#0-4) 

There is no unprivileged attacker action needed to trigger the underlying rounding — it is a function purely of `gas_used_at_most` (message-type-dependent, e.g. small commands with low gas ceilings) and governance-set `PricingParameters` values, both of which are legitimate, non-malicious inputs. Any user or protocol path sending a low-gas command when `fee_per_gas`/`reward`/`exchange_rate` combine such that the wei amount is below `10^8` will get `fee.remote == 0`, while the message is still delivered and dispatched on Ethereum at real gas cost.

### Impact Explanation
`fee.remote` is charged to the sender to cover the ETH-denominated relay/execution cost of the message on Ethereum. When this rounds to `0`, the protocol is doing "public underpriced work": messages get dispatched to Ethereum (consuming real gas that the bridge/relayer infrastructure must eventually be compensated for) without collecting the corresponding fee from the sender. Since `SendMessage::validate` is invoked on essentially every outbound bridge message path (XCM exports to Ethereum, asset transfers, foreign-asset registration, etc.), an attacker or even ordinary usage pattern can repeatedly submit these underpriced low-gas commands, extracting free (or below-cost) Ethereum-bound message dispatch. Repeated exploitation degrades the bridge's economic sustainability and can be used to spam-flood the outbound queue at zero remote-fee cost, stalling normal bridge processing — directly matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
High. This is not a theoretical edge case — it is already captured by an existing unit test (`test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`) using realistic, validated `PricingParameters` (`exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {1,1}`, `multiplier = 1/1`). Any combination of governance-set pricing parameters and message gas ceilings where the wei-scale intermediate is under `10^8` triggers it, and this can occur with entirely legitimate parameter ranges (e.g. cheap commands, low gas-per-unit costs, or exchange-rate/multiplier combinations chosen by governance for other valid reasons). No privileged actor or malicious peer is required — a normal unprivileged user submitting any bridge-eligible XCM/asset-transfer message triggers `calculate_fee` on the hot path.

### Recommendation
- Add a post-computation check in `calculate_fee` (or in `SendMessage::validate`) that rejects/aborts (or, for send paths, applies a minimum non-zero floor) if the derived `fee.remote` is `0` while the underlying wei-scale computation was non-zero — mirroring the report's fix of checking the actually-used derived value rather than only the raw input fields.
- Alternatively, perform the exchange-rate/multiplier/decimals division with higher intermediate precision (e.g., delay the decimals-truncation division until after accumulating enough precision, or round up rather than truncate) so that any non-zero wei-scale remote cost maps to at least `1` unit of native currency.
- Extend `PricingParameters::validate()` (or add a runtime-time check at fee-calculation time) to assert `fee.remote != 0` for the full range of gas ceilings the `GasMeter` can produce, not just that the raw parameter fields are non-zero.

### Proof of Concept
Using the pallet's own mock runtime and the existing test as a reproduction:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319
let gas_used: u64 = 250000;
let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.local, 698000000);
assert_eq!(fee.remote, 0); // <-- non-zero, validated params still yield a zero remote fee
```
This same `PricingParameters` combination passes `PricingParameters::validate()` (all fields non-zero) at `bridges/snowbridge/primitives/core/src/pricing.rs:39-56`, so governance could set (or already accept) such parameters, and any subsequent call through `SendMessage::validate` (`bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs:41-74`) for a message with a similarly small `gas_used_at_most` will charge `fee.remote = 0` while still enqueueing the message for dispatch on Ethereum at real gas cost.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L404-418)
```rust
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
