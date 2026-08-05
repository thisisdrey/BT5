### Title
Truncating fee-decimal conversion in `calculate_fee` can silently zero the remote relayer fee for validated pricing parameters - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Snowbridge outbound-queue fee pipeline computes a remote (Ethereum-side) fee, downcasts it, applies a multiplier/exchange-rate adjustment, and finally rescales it from 18-decimal (wei) precision to the local chain's decimals via integer division in `convert_from_ether_decimals`. Because this final step uses `checked_div` (floor division) rather than any minimum-fee check, a small — but fully valid, non-zero — combination of `PricingParameters` can produce a computed remote fee of exactly `0`. This mirrors the report's core broken invariant: a value that gates downstream acceptance (a "price"/fee that must never be non-positive to be safely treated as valid) passes through numeric conversion and is silently accepted as zero, letting the surrounding logic proceed as if payment/incentive was correctly established when it was not.

### Finding Description
`PricingParameters::validate()` in `bridges/snowbridge/primitives/core/src/pricing.rs` only guards against the *raw* fields being zero (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`): [1](#0-0) 

The pallet then derives the *final*, chargeable fee through several transformations in `calculate_fee`: [2](#0-1) 

The last step, `convert_from_ether_decimals`, floors the wei-denominated fee down to the local chain's decimal precision: [3](#0-2) 

If the intermediate wei value after multiplier/exchange-rate adjustment is smaller than `10^(18 - Decimals)` (e.g. below `1_000_000` wei for a 12-decimal chain), the division floors to `0`. No code path checks that the resulting `Fee.remote` is non-zero before it is returned from `calculate_fee` and used by `SendMessage::validate` in `send_message_impl.rs`: [4](#0-3) 

The pallet's own test suite documents this exact behavior and flags it as unintended: [5](#0-4) 

This is the direct structural analog of the external report: the guard (`validate()`) checks the *inputs* are non-zero, but never re-validates the *derived* value that is actually consumed downstream (the message-processing pipeline treats `Fee.remote == 0` as a legitimate, payable amount and proceeds to enqueue/commit/dispatch the message), exactly as the Oracle Engine in the report treated a cast/derived zero price as "valid" and continued.

### Impact Explanation
If the effective remote fee for a message is computed as `0`, the message is still committed to the merkle tree and offered for relaying to Ethereum, but the reward/gas-refund component embedded in the message (used by the Gateway contract to reimburse the relayer) is zero. No rational relayer will deliver a message that reimburses it nothing, so affected messages can pile up undelivered — a form of stalled bridge processing / under-priced public work, since the local (`WeightToFee`) queue-processing cost has already been paid by the sender and consumed a `MaxMessagesPerBlock` slot, but the message's remote leg is starved of relayer incentive. This falls squarely under the accepted impact category "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
Reaching a zero-fee outcome requires `PricingParameters` (set only via `set_pricing_parameters`, a root-only call in `pallets/system/src/lib.rs`) combined with a low `gas_used_at_most` to produce a sub-`10^(18-Decimals)` wei amount after the multiplier/exchange-rate division. Under realistic production parameters (e.g. `exchange_rate=1/400`, `fee_per_gas=20 gwei`, `rewards.remote=1 meth = 1e15 wei`) the computed value is many orders of magnitude above the truncation threshold, so the zero-fee condition is not reachable by an ordinary unprivileged user under sane governance-set parameters — it requires specific edge-case parameter values (as constructed in the pallet's own unit test) to manifest. I was not able to find any additional guard (e.g., a `MinimumFee` check or an `ensure!(fee.remote > 0)`) anywhere else in the pipeline that would catch this before commit, so the invariant genuinely goes unchecked, but triggering it depends on parameter magnitudes rather than being purely attacker-controlled at will.

### Recommendation
Add an explicit non-zero (or minimum-threshold) check on the final computed `Fee.remote` inside `calculate_fee` (or in `SendMessage::validate`) before returning it, e.g.:
```rust
ensure!(fee.remote > Zero::zero(), Error::<T>::InvalidComputedFee);
```
This prevents silently accepting a degenerate zero fee as valid, analogous to explicitly rejecting non-positive prices in the original report, rather than relying solely on validation of the raw input parameters.

### Proof of Concept
The existing pallet test already demonstrates the exact zero-fee condition end-to-end: [5](#0-4) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` despite every individual `PricingParameters` field being non-zero and passing `validate()`. This value flows unchanged into `SendMessage::validate` -> `deliver`, where the message is enqueued and later committed for Ethereum delivery with no remote incentive attached.

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-393)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L411-418)
```rust
		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

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
