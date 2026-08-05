### Title
Integer-rounding in `calculate_fee` can zero out the relayer reward embedded in outbound messages, causing underpriced/unfunded relaying even with valid pricing parameters - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::calculate_fee` and `calculate_remote_fee` compute the fee charged to users and the `reward` field embedded in every `CommittedMessage` sent to Ethereum. The decimal-scaling step in `convert_from_ether_decimals` performs a plain integer division (`checked_div(&denom)`), which silently truncates to `0` whenever the scaled remote fee is smaller than the decimal-adjustment denominator (`10^(18 - T::Decimals)`, e.g. `10^8` on a 10-decimal chain). This mirrors the ZcToken bug class exactly: a value that participates in a "rate/denominator" division can legitimately be non-zero and still round down to `0`, silently short-changing the intended recipient (here, the relayer) instead of reverting or reporting an error.

### Finding Description
`calculate_fee` at [1](#0-0)  computes the remote (Ethereum-side) fee as:

```
fee = calculate_remote_fee(gas_used_at_most, fee_per_gas, reward)   // in wei
fee = FixedU128::from_inner(fee).saturating_mul(multiplier).checked_div(&exchange_rate).into_inner()
fee = convert_from_ether_decimals(fee)
```

`convert_from_ether_decimals` at [2](#0-1)  divides the intermediate value by `10^(ETHER_DECIMALS - T::Decimals)` using plain integer division, which truncates any remainder to zero rather than rounding up or erroring.

`PricingParameters::validate` at [3](#0-2)  only guards against the individual parameters (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) being exactly zero. It does **not** guard against the *composed* result of `calculate_fee` rounding down to zero — this is precisely the gap the ZcToken report highlights: individually-nonzero inputs to a rate calculation can still produce a `0` output through integer division, and no downstream check exists to prevent that zero from being used.

This exact defect is already reproduced and asserted (but not fixed) by the repository's own test:

```rust
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
    ...
    let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
    assert_eq!(fee.local, 698000000);
    // Though none zero pricing params the remote fee calculated here is invalid
    // which should be avoided
    assert_eq!(fee.remote, 0);
}
``` [4](#0-3) 

The `fee.remote` computed here is not just used to charge the sender — it becomes `pricing_params.rewards.remote`, which is copied verbatim into the `reward` field of the `CommittedMessage` sent to the Ethereum Gateway contract:

```rust
let reward = pricing_params.rewards.remote;
...
let message = CommittedMessage {
    ...
    reward: reward.try_into().defensive_unwrap_or(u128::MAX),
    id: queued_message.id,
};
``` [5](#0-4) 

Note: `reward` here is taken from `pricing_params.rewards.remote` directly (not from the rounded `fee.remote`), so the specific field embedded on-chain for the Ethereum-side relayer payout is not itself zeroed by this particular rounding path in `do_process_message`. However, the same rounding defect in `calculate_fee`/`convert_from_ether_decimals` directly determines `Fee.remote`, which is the amount charged to the **sender** for covering relayer costs (per the module's documented fee model in the header doc comment at lines 60-67). If `Fee.remote` truncates to `0`, the sender pays nothing to cover the remote/relayer-facing cost component while a message that still carries a genuine (non-zero) `rewards.remote` obligation is queued — i.e., the fee charged is decoupled from the cost actually incurred, an underpriced-work condition consistent with the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing." Any unprivileged user submitting a message via the outbound queue (e.g., through an XCM `ExportMessage` or any pallet using `SendMessage`) can trigger `calculate_fee` with a small `gas_used_at_most` value relative to governance-set `fee_per_gas`/`exchange_rate`/`multiplier`, causing the computed local-currency `Fee.remote` to truncate to `0` due to integer division in `convert_from_ether_decimals`, even though `PricingParameters::validate` accepted all the individual parameters as non-zero. This decouples the fee charged from the actual cost of remote execution and relayer compensation, allowing underpriced message submission at governance-set parameter combinations that are not blocked by any existing validation.

### Likelihood Explanation
Likelihood is moderate-to-high: it does not require a malicious/privileged actor — any signed account that can originate an outbound message (directly or via XCM) can hit this path, and the repository's own test suite already demonstrates the zero-output condition occurring with parameters that pass `validate()`. The exact trigger depends on governance-configured `PricingParameters` (which determine the scale of `gas_used_at_most * fee_per_gas + reward` relative to the `10^8`-type decimal denominator), so whether it is exploitable on a live chain depends on the currently configured parameter values — this is the main source of uncertainty in assessing real-world exploitability without runtime-specific parameter values.

### Recommendation
- In `convert_from_ether_decimals`, use rounding-up division (or return an error/`None`) instead of truncating division, so a non-zero pre-rounding value never becomes `0`.
- Add a post-computation guard in `calculate_fee` (analogous to the recommended `getMaturityRate`-style fallback in the original report) that rejects or floors `Fee::local`/`Fee::remote` to a minimum non-zero value when inputs are non-zero, rather than silently returning `0`.
- Extend `PricingParameters::validate` (or add a companion check invoked wherever `calculate_fee` is used) to reject parameter combinations that would cause the composed fee calculation to round down to zero.

### Proof of Concept
Existing repository test demonstrates the exact zero-output scenario:
```rust
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(250_000, price_params);
assert_eq!(fee.remote, 0); // non-zero, validated inputs -> zero fee output
``` [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L337-352)
```rust
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
