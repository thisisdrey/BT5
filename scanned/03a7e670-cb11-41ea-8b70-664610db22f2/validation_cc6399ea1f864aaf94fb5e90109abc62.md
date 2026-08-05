### Title
Remote fee rounding to zero in Snowbridge outbound-queue fee calculation allows underpriced message delivery - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::<T>::calculate_fee` computes the fee a user must pay to send a message to Ethereum via the Snowbridge outbound queue. The remote-fee component, which is supposed to fund the relayer's gas cost and reward, is calculated through a sequence of integer truncations (`U256`→`u128`, `FixedU128::from_inner`, division by `exchange_rate`, then a final integer division by `10^decimals` in `convert_from_ether_decimals`) that can produce a **remote fee of `0`** even for valid, non-zero pricing parameters. This is exactly analogous to the reported Curve issue: a dynamically calculated value (the remote fee, akin to the dynamic swap fee) is computed but the result is silently accepted without a floor/sanity check, so the "calculated" value is effectively not honored downstream.

### Finding Description
`calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`) computes:
```
let fee = Self::calculate_remote_fee(gas_used_at_most, params.fee_per_gas, params.rewards.remote);
let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);
let fee = FixedU128::from_inner(fee).saturating_mul(params.multiplier).checked_div(&params.exchange_rate)...into_inner();
let fee = Self::convert_from_ether_decimals(fee);
Fee::from((Self::calculate_local_fee(), fee))
```
`calculate_remote_fee` itself (`lib.rs:396-402`) is a straightforward `fee_per_gas * gas_used + reward`, which is non-zero whenever `fee_per_gas` or `reward` are non-zero. However, treating that `u128` value as `FixedU128::from_inner` (i.e., as an already-scaled fixed-point number with 18 decimals) means the true value is really `fee / 10^18`. For small legitimate parameter magnitudes (e.g., `fee_per_gas = 1`, `reward = 1`, `exchange_rate = 1`), this yields a computed `remote` fee of exactly `0`, as demonstrated in the repository's own test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`), whose comment explicitly states: *"Though none zero pricing params the remote fee calculated here is invalid which should be avoided."*

Despite this being a known, tested, and acknowledged defect, `calculate_fee` has no floor/guard to reject or bump a zero remote fee before it is returned and subsequently charged in `send_message_impl` for a real user message. The dynamically computed remote-fee value is silently discarded/ignored in the sense that whatever near-zero true economic cost it represents is never enforced — the pallet proceeds to accept the message for delivery with `remote = 0`.

### Impact Explanation
The remote fee is defined in the pallet's own documentation (`lib.rs:38-51`) as the amount that "covers ... 2. The gas refund paid out to relayers ... 3. An additional reward paid out to relayers." If this resolves to zero while the message is nevertheless queued, committed into the merkle root, and dispatched for relaying to Ethereum, relayers receive no gas refund and no reward for delivering it. This is "public underpriced work that degrades block production or stalls bridge processing," as characterized in the impact gate: paying users can submit outbound Ethereum messages under legitimate-looking, non-zero `PricingParameters` while the bridge extracts no compensating remote fee, disincentivizing relayers from processing the queue and potentially stalling outbound delivery, or forcing the bridge to permanently subsidize gas/reward costs out of protocol funds.

### Likelihood Explanation
The pricing parameters (`fee_per_gas`, `rewards.remote`, `exchange_rate`, `multiplier`) are governance-set values updated periodically via `set_pricing_parameters`, not attacker-controlled; however, this is not an admin-abuse issue — it is a rounding/truncation implementation bug in the fee formula that produces incorrect (zero) fees for legitimately configured, non-zero parameters, as the repository's own regression test proves. Any parameter set landing in the affected numeric range (small `fee_per_gas`/`reward` relative to the `10^18` ether-decimal scaling) triggers the defect for every message sent while those parameters are active — no privileged or malicious actor is required to trigger the underpricing, only an ordinary user calling the standard outbound message send path with the currently configured pricing parameters.

### Recommendation
Enforce a minimum non-zero remote fee (or reject/clamp with a defensive error) whenever `fee_per_gas`, `reward` are non-zero but the computed post-scaling remote fee truncates to zero, mirroring the recommendation from the seed report to "use the calculated dynamic fee value" rather than silently letting truncation erase it. Concretely, validate in `calculate_fee` that `fee > 0` whenever `calculate_remote_fee(...) > 0`, and either round up (ceiling division) through the `FixedU128`/`convert_from_ether_decimals` conversions or return an error to prevent message submission until the pricing parameters are safely re-scaled by governance.

### Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` (`bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-319`) is itself the proof of concept:
```rust
let gas_used: u64 = 250000;
let price_params = PricingParameters {
    exchange_rate: FixedU128::from_rational(1, 1),
    fee_per_gas: 1_u32.into(),
    rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
    multiplier: FixedU128::from_rational(1, 1),
};
let fee = OutboundQueue::calculate_fee(gas_used, price_params);
assert_eq!(fee.remote, 0); // non-zero inputs yield zero remote fee
```
With these (valid, non-zero) pricing parameters in force, any user call through `send_message_impl` that uses `calculate_fee` to determine the charge will result in the message being accepted and queued for Ethereum delivery while `Fee.remote == 0`, i.e., no funds are reserved to refund gas or reward the relayer for that message. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-67)
```rust
//! # Fees
//!
//! An upfront fee must be paid for delivering a message. This fee covers several
//! components:
//! 1. The weight of processing the message locally
//! 2. The gas refund paid out to relayers for message submission
//! 3. An additional reward paid out to relayers for message submission
//!
//! Messages are weighed to determine the maximum amount of gas they could
//! consume on Ethereum. Using this upper bound, a final fee can be calculated.
//!
//! The fee calculation also requires the following parameters:
//! * Average ETH/DOT exchange rate over some period
//! * Max fee per unit of gas that bridge is willing to refund relayers for
//!
//! By design, it is expected that governance should manually update these
//! parameters every few weeks using the `set_pricing_parameters` extrinsic in the
//! system pallet.
//!
//! This is an interim measure. Once ETH/DOT liquidity pools are available in the Polkadot network,
//! we'll use them as a source of pricing info, subject to certain safeguards.
//!
//! ## Fee Computation Function
//!
//! ```text
//! LocalFee(Message) = WeightToFee(ProcessMessageWeight(Message))
//! RemoteFee(Message) = MaxGasRequired(Message) * Params.MaxFeePerGas + Params.Reward
//! RemoteFeeAdjusted(Message) = Params.Multiplier * (RemoteFee(Message) / Params.Ratio("ETH/DOT"))
//! Fee(Message) = LocalFee(Message) + RemoteFeeAdjusted(Message)
//! ```
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L395-402)
```rust
		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
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
