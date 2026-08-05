## Analysis

The Boot Finance bug's core invariant is: **a multiplier/precision value that should scale a price is computed via `mul().div()` on raw integers, and when the target value is smaller than the implicit precision base (`10**18`), integer division truncates the multiplier to zero**, silently breaking the pool's economic accounting while leaving the contract otherwise "valid" (non-reverting).

The closest local analog is in Snowbridge's outbound-queue fee calculation, which chains the same kind of implicit fixed-point rescaling and can silently truncate the remote relayer fee to zero even though every pricing parameter is valid and non-zero.

### Title
Snowbridge outbound-queue `calculate_fee` can round the remote relayer reward to zero for legitimately-priced messages, allowing underpriced bridge delivery - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
`Pallet::calculate_fee` derives the DOT/KSM-denominated relayer reward by treating a raw wei integer as the *inner* value of a `FixedU128` (implicitly dividing by `10**18`), performing fixed-point multiply/divide against `multiplier`/`exchange_rate`, and then dividing the result again by a decimal-adjustment denominator (`10**(18 - Decimals)`) in `convert_from_ether_decimals`. When the wei-denominated remote fee is smaller than that decimal denominator, the final integer division truncates to zero, exactly like the reported `customPrecisionMultipliers` rounding to zero when `_targetPrice < 10**18`.

### Finding Description
`calculate_fee` at [1](#0-0)  computes:

```
fee = FixedU128::from_inner(remote_fee_wei)
        .saturating_mul(multiplier)
        .checked_div(&exchange_rate)
        .into_inner();
fee = convert_from_ether_decimals(fee);
```

`convert_from_ether_decimals` then performs a final plain integer division: [2](#0-1) 

`remote_fee_wei = fee_per_gas * gas_used_at_most + reward` (computed in `calculate_remote_fee`, [3](#0-2) ). Because `FixedU128::from_inner` treats the wei integer as already scaled by `1e18`, and the subsequent `checked_div` by a unit exchange rate leaves the inner value numerically unchanged, the pre-decimals fee retains the *raw wei magnitude*. `convert_from_ether_decimals` then divides that raw wei magnitude by `10^(18 - Decimals)` (`1e6` for a 12-decimal chain, `1e8` for a 10-decimal chain). Any `remote_fee_wei` below that denominator floor to **zero**, even though `PricingParameters::validate()` guarantees `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, and `multiplier` are all non-zero: [4](#0-3) .

This exact scenario is already reproduced in the pallet's own test suite, with a comment acknowledging it is a defect: [5](#0-4) 

None of `validate()`, `calculate_fee`, or `convert_from_ether_decimals` reject or floor a fee that rounds to zero — the `checked_div`/`expect` calls only guard against division-by-zero of the *divisor*, not against the *numerator* being too small to survive the decimal rescale.

### Impact Explanation
`Fee.remote` is the reward promised to off-chain relayers for delivering a message to Ethereum (documented in the module fee-computation spec at [6](#0-5) ). If this value silently computes to zero for a class of commands/messages whose `max_dispatch_gas` and configured `fee_per_gas`/`reward` combination produce a wei amount below the decimal-adjustment denominator, the local fee is still charged to the sender and the message is still committed into the Merkle root and queued for Ethereum delivery, but relayers have no economic incentive to submit it. This degrades/stalls bridge delivery processing (the "public underpriced work that ... stalls bridge processing" impact bucket): messages accumulate in the committed-but-unrelayed state, defeating the purpose of the reward mechanism without any error, revert, or governance misconduct — the pricing parameters can be entirely sane and validated, yet the derived per-message fee still degenerates to zero due to the chained rescale.

### Likelihood Explanation
This is not a hypothetical: the exact rounding-to-zero condition is reproduced deterministically in `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` [5](#0-4)  using entirely valid, non-zero `PricingParameters`. Any combination of `fee_per_gas`, `Command::max_dispatch_gas` (from `GasMeter::maximum_dispatch_gas_used_at_most`), and `rewards.remote` whose product/sum in wei is smaller than `10^(18-Decimals)` triggers it — this depends only on chain-side constants and the message's command type, not on any privileged or malicious actor, satisfying the "unprivileged attacker/ordinary usage" bar.

### Recommendation
Guard the fee computation against zero-rounding, analogous to the recommended Boot Finance fix of adding extra precision headroom:
- After computing the pre-decimals fixed-point fee, verify the result is non-zero before/after the final `checked_div` in `convert_from_ether_decimals`, and if it truncates to zero, either reject fee calculation (return an error) or round up (ceil) rather than floor, so relayers are never promised a zero reward for genuinely fee-bearing messages.
- Alternatively, perform the decimal rescale in the same higher-precision (`U256`/`FixedU128`) domain as the multiplier/exchange-rate math instead of via raw integer division after `.into_inner()`, so small legitimate wei rewards are not lost to the decimals conversion.

### Proof of Concept
Using the pallet's own mock config (`Decimals = ConstU8<12>`), reuse the existing failing test as the PoC: [5](#0-4) 
With `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_fee` returns `fee.remote == 0` despite all pricing parameters passing `PricingParameters::validate()` as non-zero — demonstrating that a message can be committed and queued for Ethereum delivery while the relayer reward silently rounds to zero.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L60-67)
```rust
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L396-402)
```rust
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
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
