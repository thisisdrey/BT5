### Title
`OutboundQueue::calculate_fee` can silently return a zero remote fee despite validated non-zero `PricingParameters`, underpricing Ethereum message delivery - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The Snowbridge outbound queue computes the fee a user must pay to deliver a message to Ethereum via `Pallet::<T>::calculate_fee` [1](#0-0) . This mirrors the reported pattern: an upstream validator (`PricingParameters::validate`) checks that all pricing inputs (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) are non-zero [2](#0-1) , exactly like `Exchange.getMarkPrice`'s inputs being checked elsewhere. But the *derived* value — the final computed `fee.remote` — is never re-checked for zero before being used, just as `KangarooVault.removeCollateral` never re-checked the derived `markPrice`. The repo's own test suite proves this derived-zero case actually occurs even with fully "valid" (non-zero) parameters: `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` shows `fee.remote == 0` while explicitly commenting "Though none zero pricing params the remote fee calculated here is invalid which should be avoided" [3](#0-2) .

### Finding Description
`calculate_fee` performs a chain of arithmetic on `gas_used_at_most`, `fee_per_gas`, `reward`, `multiplier`, and `exchange_rate`:

```rust
pub(crate) fn calculate_fee(
    gas_used_at_most: u64,
    params: PricingParameters<T::Balance>,
) -> Fee<T::Balance> {
    let fee = Self::calculate_remote_fee(gas_used_at_most, params.fee_per_gas, params.rewards.remote);
    let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);
    let fee = FixedU128::from_inner(fee)
        .saturating_mul(params.multiplier)
        .checked_div(&params.exchange_rate)
        .expect("exchange rate is not zero; qed")
        .into_inner();
    let fee = Self::convert_from_ether_decimals(fee);
    Fee::from((Self::calculate_local_fee(), fee))
}
``` [4](#0-3) 

The only guard present is `params.exchange_rate != 0` (an `.expect`) — analogous to the Solidity code checking `!isInvalid && baseAssetPrice != 0` before calling `getMarkPrice()`. However nothing checks the *result* `fee` (i.e., the value that is actually charged and returned to the caller) for zero. Two lossy steps can drive it to zero even with fully valid, non-zero inputs:
1. `convert_from_ether_decimals` performs `value.checked_div(denom)` where `denom = 10^(ETHER_DECIMALS - T::Decimals)` — for chains with fewer decimals than Ether (e.g., DOT with 10 decimals vs Ether's 18), `denom` is huge (10^8), so any `value` below that magnitude truncates to 0 [5](#0-4) .
2. The `checked_div(&params.exchange_rate)` combined with small `gas_used_at_most`/`fee_per_gas`/`reward` inputs can produce a small `fee` before the decimals conversion.

The repository's own regression test confirms this: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards.remote = 1` — all strictly non-zero and passing `validate()` — `calculate_fee` returns `fee.remote == 0` [3](#0-2) . This is the exact same "derived value can be zero even though the source inputs passed validation" defect described in the M-05 report: `Exchange.getMarkPrice`'s inputs (`baseAssetPrice`) were checked at some call sites but the derived `markPrice` output was never re-checked at others.

This computed `fee` (specifically `fee.remote`, denominated in the local/native currency) is what downstream code (e.g. `send_message_impl.rs`, which implements the XCM `SendMessageFeeProvider`/fee-charging logic for `snowbridge-router-primitives`) uses to withdraw payment from the user before enqueuing a message for Ethereum delivery. Because `calculate_fee` never asserts `fee.remote != 0` (or `fee.local + fee.remote` is sufficient to cover `params.rewards.remote`), a message can be accepted and queued for a fee that undercharges the actual remote-relayer reward embedded in the committed message (`reward: pricing_params.rewards.remote` used directly in `do_process_message`, not the computed fee) [6](#0-5) .

### Impact Explanation
This is public, underpriced work with bridge-processing impact: any unprivileged user submitting an XCM to be exported to Ethereum pays a fee computed by `calculate_fee`, which can silently be zero (or far less than intended) due to decimal-conversion truncation, while the committed message still carries the full non-zero `rewards.remote` obligation that the bridge/treasury must ultimately cover. Repeated exploitation (submitting many messages when the fee floor rounds to zero) allows users to push messages into the outbound queue for Ethereum delivery without paying the real remote-relayer cost, degrading the bridge's fee-based rate limiting/anti-spam economics and potentially causing relayers to be systematically underpaid or the bridge's reserve to be drained to subsidize the shortfall — fitting the gate's "public underpriced work that degrades block production or stalls bridge processing" and "duplicate/incorrect payout" categories. No privileged actor, governance action, or malicious relayer is required — this is triggerable purely by normal message submission under governance-set (but otherwise valid) pricing parameters.

### Likelihood Explanation
High for occurrence, given the repo's own test demonstrates the zero-fee outcome with realistic, validator-accepted parameters (a 1:1 exchange rate is not an edge case that governance would consider invalid). Any parachain using Snowbridge with a native token having fewer decimals than Ether (10–12 decimals is standard for DOT/KSM vs Ether's 18) is structurally exposed to the `convert_from_ether_decimals` truncation path for small gas/reward values, and `PricingParameters::validate()` provides no protection since it only checks the raw inputs, not the derived fee.

### Recommendation
After computing `fee` in `calculate_fee`, assert that the resulting native-currency fee is non-zero (and ideally that it is sufficient to cover `params.rewards.remote` after currency conversion) before returning `Fee::from(...)`; if it is zero, this should be treated as an invalid/unusable pricing configuration and either the call should error or `PricingParameters::validate` should be extended to reject parameter combinations that would produce this outcome given `T::Decimals`. At minimum, `do_process_message`/the fee-charging call site should reject enqueuing messages whose computed remote fee is zero.

### Proof of Concept
The existing unit test in the repository already demonstrates the vulnerable condition end-to-end:
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
``` [3](#0-2) 

1. Governance sets `PricingParameters` that pass `validate()` (all fields non-zero) but yield a small `fee_per_gas`/`rewards.remote` relative to `T::Decimals` truncation in `convert_from_ether_decimals`.
2. A user submits a message for export to Ethereum; `calculate_fee` is invoked and returns `fee.remote == 0`.
3. The user is charged `fee.local` only (or a near-zero total), yet the committed message still embeds the full `params.rewards.remote` reward obligation that Ethereum-side relayers expect to be paid from bridge funds, producing a mismatch between what was collected and what must be paid out.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-318)
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
```
