### Title
Fee decimal down-scaling in `convert_from_ether_decimals` silently drops the up-scaling case for local currencies with >18 decimals - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`OutboundQueue::calculate_fee` converts the ether-denominated (18-decimal) remote fee into the pallet's native `T::Balance` units via `convert_from_ether_decimals`. The conversion assumes the local currency always has *fewer or equal* decimals than Ether's 18, exactly the "≤18-decimals" assumption that the external report calls out as the root cause of the `DECIMAL_MULTIPLIER` bug. When the local currency's decimal count is configured to exceed 18, the function silently collapses to a no-op scale factor instead of scaling the value up, producing a fee that is too small by a factor of `10^(T::Decimals - 18)`.

### Finding Description
`convert_from_ether_decimals` computes the divisor using `saturating_sub`: [1](#0-0) 

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
`ETHER_DECIMALS` is a fixed constant equal to 18: [2](#0-1) 

The subtraction `ETHER_DECIMALS.saturating_sub(T::Decimals::get())` is only correct for `T::Decimals::get() <= 18`. If a chain configures `T::Decimals` (the local/native currency decimal precision passed into this pallet's `Config`) to a value **greater** than 18, `saturating_sub` clamps the result to `0`, so `denom` becomes `10^0 = 1`, and the function returns the raw 18-decimal ether value unscaled — instead of *multiplying* the value by `10^(T::Decimals - 18)` to correctly express it in the local currency's higher-precision units. This is the exact bug class from the external report: a decimal-normalization multiplier that only ever divides (handles the "fewer decimals" branch) and has no branch to multiply for tokens with more decimals than the reference (18, analogous to the reported contract's 18-decimal assumption).

By contrast, other parts of this same repository correctly handle both directions of decimal scaling with explicit branches, showing the intended pattern that `convert_from_ether_decimals` is missing: [3](#0-2) 

### Impact Explanation
`calculate_fee` is the function that determines how much native currency a user must pay to have `pallet_outbound_queue` deliver a message to Ethereum, covering the remote (Ethereum-side) execution/reward cost: [4](#0-3) 

If the divisor collapses to `1` because `T::Decimals::get() > 18`, the computed remote fee is under-charged by a factor of `10^(T::Decimals - 18)`. This falls squarely into the "public underpriced work that degrades block production or stalls bridge processing" category in the impact gate: messages would be accepted for delivery/relaying while systematically under-funding the relayer reward and remote execution cost, letting an unprivileged sender submit outbound bridge messages far below their true cost and starve/degrade the bridge's economic security.

### Likelihood Explanation
This is not exploitable on the currently deployed BridgeHub configurations because Polkadot (10 decimals) and Kusama (12 decimals) both satisfy `T::Decimals::get() <= 18`, so `saturating_sub` never clamps in production today. The defect is real and locally provable in the code (no branch exists to correctly multiply when `T::Decimals > 18`), and would become live/exploitable the moment any Snowbridge-integrated chain configures the outbound queue pallet with a native currency of more than 18 decimals — which the pallet's `Config` trait does not forbid. Given the external report's own point that "there's no hard constraint on the decimals," this configuration is plausible for future deployments and is a genuine gap the code does not defend against, unlike `pallet-psm`'s explicit `MAX_DECIMALS_DIFF` and bidirectional scaling.

### Recommendation
Replace the one-directional `saturating_sub`/divide-only logic with a bidirectional conversion (mirroring `pallet_psm::external_to_internal`/`internal_to_external`): when `T::Decimals::get() > ETHER_DECIMALS`, multiply by `10^(T::Decimals - ETHER_DECIMALS)` instead of dividing by `10^0`. Add a compile-time or `validate()`-style guard rejecting/handling `T::Decimals::get() > 18` explicitly, and add a regression test analogous to `bridges/snowbridge/pallets/outbound-queue/src/test.rs` covering a hypothetical `T::Decimals = 24` configuration.

### Proof of Concept
1. Configure a runtime instance of `snowbridge_pallet_outbound_queue::Config` with `type Decimals = ConstU8<20>` (a hypothetical >18-decimal native currency).
2. Call `OutboundQueue::calculate_fee(gas_used_at_most, params)` with realistic `PricingParameters` (as in the existing tests `test_calculate_fees_with_unit_multiplier` / `test_calculate_fees_with_multiplier`): [5](#0-4) 
3. Inside `convert_from_ether_decimals`, `decimals = 18u8.saturating_sub(20) = 0`, so `denom = 1`, and the function returns the raw 18-decimal-scaled fee value unchanged — instead of the value multiplied by `10^2` needed to express it correctly in the 20-decimal native currency. The resulting `fee.remote` charged to the user is `100x` smaller than the correct amount, letting outbound messages be dispatched for a fraction of their true remote-delivery cost.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v1/message.rs (L386-386)
```rust
pub const ETHER_DECIMALS: u8 = 18;
```

**File:** substrate/frame/psm/src/lib.rs (L1580-1599)
```rust
		pub(crate) fn external_to_internal(
			amount: BalanceOf<T>,
			ext_decimals: u8,
			internal_decimals: u8,
		) -> Result<BalanceOf<T>, Error<T>> {
			use core::cmp::Ordering::*;
			match ext_decimals.cmp(&internal_decimals) {
				Equal => Ok(amount),
				Less => {
					let diff = (internal_decimals - ext_decimals) as u32;
					let factor = Self::pow10(diff)?;
					amount.checked_mul(&factor).ok_or(Error::<T>::ConversionOverflow)
				},
				Greater => {
					let diff = (ext_decimals - internal_decimals) as u32;
					let factor = Self::pow10(diff)?;
					Ok(amount.checked_div(&factor).unwrap_or_else(BalanceOf::<T>::zero))
				},
			}
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L271-301)
```rust
#[test]
fn test_calculate_fees_with_unit_multiplier() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 400),
			fee_per_gas: 10000_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params);
		assert_eq!(fee.local, 698000000);
		assert_eq!(fee.remote, 1000000);
	});
}

#[test]
fn test_calculate_fees_with_multiplier() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 400),
			fee_per_gas: 10000_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(4, 3),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params);
		assert_eq!(fee.local, 698000000);
		assert_eq!(fee.remote, 1333333);
	});
}
```
