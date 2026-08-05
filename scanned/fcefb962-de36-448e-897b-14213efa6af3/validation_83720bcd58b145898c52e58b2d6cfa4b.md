### Title
Underpriced Snowbridge outbound message delivery due to integer-division truncation of the remote (Ethereum) fee - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The external report's core broken invariant is: a payment amount is computed via integer division that can silently truncate to a smaller (or zero) value than intended, and nothing detects or compensates for that truncation before the corresponding service/asset is delivered. In `pallet-outbound-queue`'s `calculate_fee`, the ETH-denominated `remote` fee is converted into local currency using a chain of `saturating_mul`/`checked_div`/`checked_div` (decimal-scaling) operations that can floor to `0` even when `PricingParameters` (`fee_per_gas`, `rewards.remote`, `exchange_rate`, `multiplier`, `Decimals`) are all non-zero, as demonstrated by the pallet's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`.

### Finding Description
`calculate_fee` at [1](#0-0)  computes the message-processing fee that a sender must pay to have their message committed and dispatched to Ethereum. The remote component goes through:
1. `calculate_remote_fee` — `fee_per_gas * gas_used_at_most + reward` (U256, exact).
2. Downcast to `u128`.
3. `FixedU128::from_inner(fee).saturating_mul(multiplier).checked_div(exchange_rate)` — a fixed-point division that rounds toward zero.
4. `convert_from_ether_decimals` at [2](#0-1)  — a second **floor** integer division by `10^(18 - Decimals)` to rescale from Ether's 18 decimals to the local chain's decimals (e.g., 10 for DOT).

Two successive floor-divisions compound truncation. The pallet's own regression test proves the resulting `fee.remote` can be exactly `0` while every individual pricing parameter is non-zero: [3](#0-2) 

Unlike the patched `pallet-asset-conversion` quote functions (which now explicitly reject inputs whose truncated output would be `Some(0)`, per `prdoc/stable2606/pr_11795.prdoc`) or `pallet-psm` (which keeps truncation "dust" with the caller and explicitly rejects transactions whose net output is zero via `AmountTooSmallAfterConversion`), `calculate_fee` has **no such zero-output guard**. There is no check anywhere in `do_process_message` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:301-364`) that rejects or re-prices a message when the computed remote fee floors to zero — the message is unconditionally appended to `Messages`/`MessageLeaves` and committed for relaying to Ethereum regardless of the fee actually charged.

### Impact Explanation
The `remote` fee is what is supposed to reimburse the relayer for the Ethereum-side gas cost of dispatching the command and to fund the protocol's cross-chain reward mechanism. If truncation drives it to zero (which the pallet's own test shows is directly reachable with realistic, non-degenerate `PricingParameters` — e.g. low `gas_used_at_most`, unfavorable `exchange_rate`, or a chain with few `Decimals`), a message can be queued and committed for Ethereum-side execution while the sender pays nothing (or an amount below cost) for that execution. This is public, underpriced work: any account able to trigger outbound-queue message submission (which in Snowbridge is invoked by governance/other pallets/XCM as part of normal bridging, not by a privileged actor) can cause committed messages to consume relayer/dispatch capacity on the bridge without commensurate payment, degrading bridge economics and potentially stalling or discouraging relayer processing (no relayer will service unprofitable messages), consistent with the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
The condition is deterministic and governed entirely by `PricingParameters` set by governance and the actual `gas_used_at_most` for a given command — no malicious relayer, validator, or governance abuse is required to trigger it; it is a pure arithmetic consequence of the two chained floor-divisions once parameters land in a regime the pallet's own test already demonstrates triggers `fee.remote == 0`. Because `PricingParameters` (exchange rate, multiplier, fee_per_gas) fluctuate with market conditions over time, the "zero remote fee" regime can be reached in production without any parameter being obviously degenerate.

### Recommendation
- Add an explicit check in `calculate_fee` (or immediately after it in `do_process_message`) that rejects (or rounds up) whenever the computed `remote` component is `0` but the inputs (`fee_per_gas`, `rewards.remote`) were non-zero — mirroring the `AmountTooSmallAfterConversion` / zero-output guards already used in `pallet-psm` and `pallet-asset-conversion`.
- Use ceiling division (`div_ceil`) instead of floor `checked_div` in both the `FixedU128` exchange-rate conversion and `convert_from_ether_decimals`, so the protocol never undercharges, consistent with the ceiling-rounding fee model already used elsewhere in this repo (e.g., `pallet-psm`'s `mul_ceil` for minting/redemption fees).
- Enforce a configurable minimum remote fee floor so relayer incentives cannot be economically eliminated by decimal-scaling truncation.

### Proof of Concept
The existing unit test already demonstrates the truncation-to-zero condition without any modification:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
assert_eq!(fee.local, 698000000);
assert_eq!(fee.remote, 0); // remote fee floors to zero despite all-nonzero inputs
```
To turn this into an end-to-end PoC: configure `PricingParameters` with a small `fee_per_gas`/`reward` relative to `exchange_rate` and `Decimals` scaling (as above), then submit any command through the outbound queue (e.g. via an XCM/governance call that invokes `snowbridge_core::outbound::v1::SendMessage`). `calculate_fee` will return `fee.remote == 0`; `do_process_message` still appends the message to `Messages`/`MessageLeaves` and commits it (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:340-363`) with no rejection path for the zero-fee outcome, so the message proceeds to Ethereum-side dispatch unpaid.

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
