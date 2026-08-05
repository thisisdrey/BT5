### Title
Outbound queue fee-to-remote-currency conversion truncates to zero for small remote fees, permitting free/underpriced message delivery — (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` computes the remote-currency (ETH) delivery fee and converts it into the local (parachain) currency via `convert_from_ether_decimals`, which performs an integer division scaled by `10^(ETHER_DECIMALS - T::Decimals::get())`. This is the exact bug class described in the external report: when the fixed-point value being converted is smaller than the computed divisor (a direct consequence of the local chain's `Decimals` being far fewer than Ethereum's 18), the division truncates to zero, silently producing a `0` fee component instead of an error or a saturated minimum.

### Finding Description
`calculate_fee` at [1](#0-0)  computes the remote fee in wei, downcasts it, multiplies by `params.multiplier`, divides by `params.exchange_rate`, and finally calls `convert_from_ether_decimals` to rescale the 18-decimal Ethereum-denominated fixed point value into the chain's native `Decimals` (10 for DOT, 12 for KSM): [2](#0-1) 

`denom = 10^(18 - T::Decimals)` is a very large constant (e.g. `10^8` for a 10-decimal chain). `value.checked_div(denom)` floor-divides; if `value < denom`, the result is `0`. Unlike `pallet-psm`'s decimal-conversion helpers elsewhere in this repo (which use `checked_mul`/ceiling rounding and explicit `AmountTooSmallAfterConversion` guards), `convert_from_ether_decimals` has **no minimum-fee floor and no error path** for this truncation — it simply returns `T::Balance::zero()` via `.into()`.

This is corroborated by the pallet's own test suite, which explicitly demonstrates and flags the issue: [3](#0-2) 

The test passes *non-zero* `PricingParameters` (`fee_per_gas = 1`, `rewards.remote = 1`) — parameters that pass `PricingParameters::validate()` (which only rejects hard zero values) — yet `calculate_fee` returns `fee.remote == 0`. The test comment itself states: "Though none zero pricing params the remote fee calculated here is invalid which should be avoided."

### Impact Explanation
`calculate_fee`'s output is the fee actually charged/reserved for delivering a message from Substrate to Ethereum via the outbound queue. If the remote-fee component silently truncates to zero, an unprivileged user submitting XCM/system messages through the outbound queue can have their message accepted and queued for delivery without contributing the ether-denominated component meant to reimburse the relayer's gas cost on Ethereum. This is public underpriced (here, literally free) work: attackers can flood `do_process_message`/the outbound queue with messages up to `MaxMessagesPerBlock` at zero marginal remote cost, degrading bridge throughput, griefing relayers who must front gas for undercompensated deliveries, and potentially stalling bridge message processing — matching the program's explicitly in-scope impact "public underpriced work that degrades block production or stalls bridge processing."

### Likelihood Explanation
No malicious peer, relayer, validator, or governance action is required — this is triggered purely by ordinary parameter configuration (any chain with `Decimals` sufficiently smaller than 18, combined with a low enough `fee_per_gas`/`exchange_rate`/`multiplier` product) and normal user-facing message submission. The condition is deterministic arithmetic truncation, not a probabilistic race, and is already demonstrated by an existing unit test in the repository, indicating the maintainers are aware the scenario is reachable with valid, non-zero pricing parameters.

### Recommendation
Replace the floor `checked_div` in `convert_from_ether_decimals` with rounding-up division (or reuse the `multiply_by_rational_with_rounding`/`Rounding::Up` helpers already used elsewhere in this codebase, e.g. in `substrate/primitives/arithmetic/src/helpers_128bit.rs`), and additionally enforce a non-zero floor analogous to `pallet-psm`'s `AmountTooSmallAfterConversion` check or `asset-tx-payment`'s `min_converted_fee` pattern, so that any non-zero pre-conversion fee component maps to at least `1` post-conversion instead of `0`.

### Proof of Concept
The existing test already constitutes a proof of concept:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.remote, 0); // non-zero, validated pricing params yield a zero remote fee
```
With `fee.remote == 0`, any user-submitted message still passes fee validation/charging logic keyed on this value, allowing message submission without the intended remote-fee contribution.

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
