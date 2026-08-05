All findings confirmed exactly as described in the claim: `calculate_fee` at [1](#0-0)  uses `convert_from_ether_decimals` which performs plain truncating integer division at [2](#0-1) , `PricingParameters::validate` only rejects exact-zero fields and not truncated-to-zero outcomes at [3](#0-2) , and the repo's own regression test at [4](#0-3)  proves `fee.remote == 0` with fully valid non-zero pricing parameters. The committed message still carries the full untruncated `reward`/`max_fee_per_gas` from `PricingParameters` in `do_process_message` at [5](#0-4) , confirming the mismatch between what's collected and what's promised to relayers.

Audit Report

## Title
Integer-division truncation in `OutboundQueue::calculate_fee` lets the remote (Ethereum-side) delivery fee round down to zero, causing underpriced bridge work - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
The Snowbridge outbound queue's `Pallet::calculate_fee` converts an 18-decimal wei-denominated remote fee into native decimals via `convert_from_ether_decimals`, which performs plain truncating integer division with no rounding-up or minimum floor. When `gas_used_at_most * fee_per_gas + reward` is small relative to the decimals-adjustment divisor, `fee.remote` truncates to exactly zero even though `PricingParameters::validate()` accepts the parameters as fully non-zero.

## Finding Description
`calculate_fee` computes the remote fee in wei via `calculate_remote_fee`, scales it by `multiplier`/`exchange_rate`, then calls `convert_from_ether_decimals`, which does `value.checked_div(denom)` with `denom = 10^(ETHER_DECIMALS - Decimals)` and no rounding or floor logic [6](#0-5) . `PricingParameters::validate` only rejects exact-zero `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` — it has no check that the resulting fee computation avoids truncation to zero [3](#0-2) . The repository's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` demonstrates this exact scenario, explicitly commenting "the remote fee calculated here is invalid which should be avoided" [4](#0-3) . This truncated fee is returned from `SendMessage::validate` and used to charge the sender via `Fee::total()`, while the actual committed message queued for Ethereum delivery still carries the full, untruncated `reward` and `max_fee_per_gas` pulled directly from `PricingParameters` in `do_process_message` [5](#0-4) .

## Impact Explanation
Any unprivileged user sending a message through the bridge (via XCM export or `snowbridge_pallet_system::Pallet::send`) can, under governance-set `PricingParameters` and `Decimals` combinations that yield a small `gas_used_at_most * fee_per_gas + reward` relative to the decimals divisor, pay `fee.remote = 0` while the bridge still commits to paying relayers on Ethereum for that message. This matches the accepted impact category of "public underpriced work that degrades block production or stalls bridge processing," since repeated occurrences drain relayer-reward funding without collecting corresponding fees.

## Likelihood Explanation
The likelihood is conditional on the specific `PricingParameters` (`exchange_rate`, `fee_per_gas`, `multiplier`, `rewards.remote`) and `Decimals` configured by governance, which the pallet's own documentation says are manually updated "every few weeks" [7](#0-6) . Whenever that combination produces a scaled value below the decimals divisor, every message sent during that window is affected — this is not a contrived edge case, since the maintainers wrote a dedicated regression test reproducing it exactly.

## Recommendation
Modify `convert_from_ether_decimals` to round up (ceiling division) instead of truncating, or introduce a configurable minimum non-zero fee floor so a strictly positive computed fee cannot collapse to zero after decimal conversion. Additionally, extend `PricingParameters::validate` or add a runtime check in `calculate_fee` to detect/reject parameter combinations that would produce `fee.remote == 0` for realistic `gas_used_at_most` values.

## Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` reproduces this: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1`, and `gas_used = 250000`, calling `OutboundQueue::calculate_fee(250000, price_params)` yields `fee.local = 698000000` (correctly nonzero) but `fee.remote = 0`, despite all pricing parameters passing `validate()` [4](#0-3) .

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L49-55)
```rust
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-418)
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
