Based on my investigation, the strongest local analog to the Chainlink price-data bug is in Snowbridge's outbound-queue fee pricing calculation, where integer-division truncation in the chained `FixedU128`/`checked_div` arithmetic can silently produce a `remote` fee of `0` even when all pricing parameters are validated as non-zero — a fact the pallet's own test suite documents but does not fix.

### Title
Fee calculation in `calculate_fee` can round the Ethereum-side (remote) fee to zero via truncating integer division, causing underpriced bridge messages - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
`Pallet::<T>::calculate_fee` converts a raw wei value into `FixedU128`, multiplies by `multiplier`, divides by `exchange_rate`, and then performs a second `u128::checked_div` in `convert_from_ether_decimals` to rescale from Ether's 18 decimals to the local currency's decimals [1](#0-0) . Both divisions truncate toward zero rather than rounding up, so a legitimately non-zero remote cost can be computed as `0`, meaning the sender pays nothing for the Ethereum-side gas/reward component of message delivery. The pallet's own test explicitly reproduces and flags this as invalid behavior: [2](#0-1) .

### Finding Description
`calculate_fee` computes the remote (Ethereum-side) fee as follows:
1. `calculate_remote_fee` returns `fee_per_gas * gas_used_at_most + reward` in wei (`U256`) [3](#0-2) .
2. That value is downcast to `u128` and reinterpreted as the *inner* representation of a `FixedU128` (i.e., treated as already scaled by `10^18`), then multiplied by `multiplier` and divided by `exchange_rate` via `checked_div` [4](#0-3) .
3. The result is passed to `convert_from_ether_decimals`, which does a second, plain integer `checked_div` by `10^(ETHER_DECIMALS - T::Decimals)` to rescale from 18-decimal Ether precision down to the chain's native decimals (e.g., 10 for Polkadot) [5](#0-4) .

Both division steps use floor/truncating integer division. When the computed wei fee is small relative to the divisor(s) — e.g., low `fee_per_gas`, low `gas_used_at_most` for cheap commands, or a governance-set `exchange_rate`/`multiplier` combination that shrinks the value — the final `remote` fee rounds down to `0`, even though `PricingParameters::validate` guarantees every individual parameter (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) is non-zero [6](#0-5) . `validate()` only checks that the *inputs* are non-zero; it never checks that the *derived* fee stays non-zero after the two truncating divisions, so this guard does not stop the path.

This is functionally the same defect class as the reported bug: a monetary value derived from an external price/rate input is manipulated through chained arithmetic without accounting for the output format/precision, and no equivalent of `SafeMath`-style rounding protection (e.g., rounding up, or a floor check) is applied.

### Impact Explanation
`calculate_fee` determines the fee charged to whoever sends a message through the outbound queue to cover Ethereum-side gas refunds and relayer rewards [7](#0-6) . If the computed `remote` component silently truncates to `0`, the sender is charged nothing for that portion while the message still carries a real (non-zero) `reward` field embedded in the committed message and consumes real gas on Ethereum. This falls under the "public underpriced work that degrades block production or stalls bridge processing" impact category: users could systematically issue commands whose gas/reward footprint is undercharged, degrading the economic viability of relaying and creating a persistent mismatch between what senders pay and what the bridge economically requires to function.

### Likelihood Explanation
This is a narrow, parameter-dependent edge case. The `PricingParameters` (`exchange_rate`, `fee_per_gas`, `multiplier`, `rewards.remote`) are set by governance via `set_pricing_parameters`, not by an arbitrary unprivileged attacker, and the test that demonstrates the truncation-to-zero (`test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero`) uses extreme values (`fee_per_gas = 1`, `reward = 1`) unlikely in production configurations like the one deployed for Bridge Hub Westend/Kusama where `fee_per_gas = gwei(20)` and `rewards.remote = meth(1)` [8](#0-7) . With realistic parameters the truncation is unlikely to fully zero out the fee, but the underlying rounding-direction defect is real, has zero validation guarding the derived output, and could become exploitable if governance ever sets low-magnitude pricing parameters (e.g., during a fee-reduction adjustment) or for commands with very low `max_dispatch_gas`.

### Recommendation
- Round the two `checked_div` operations in `calculate_fee`/`convert_from_ether_decimals` up (ceiling division) rather than truncating down, so the computed fee can never be strictly less than the actual cost.
- After computing the final `remote` fee, assert/return an error if the result is `0` while any of `fee_per_gas`, `gas_used_at_most`, or `reward` was non-zero, rather than silently accepting a zero fee.
- Extend `PricingParameters::validate` (or add a post-computation check in `calculate_fee`) to reject configurations that would produce a zero derived fee for realistic message weights.

### Proof of Concept
The existing regression test already demonstrates the flaw end-to-end:
```rust
// bridges/snowbridge/pallets/outbound-queue/src/test.rs:303-318
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
``` [2](#0-1) 

This confirms that `PricingParameters::validate()` (all fields non-zero) does not prevent `calculate_fee` from returning a zero `remote` fee, matching the reported bug class of incorrect/underpriced value derivation from chained arithmetic on external price-like inputs.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L38-70)
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
//!
//! By design, the computed fee includes a safety factor (the `Multiplier`) to cover
//! unfavourable fluctuations in the ETH/DOT exchange rate.
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

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/bridge_to_ethereum_config.rs (L88-96)
```rust
parameter_types! {
	pub const CreateAssetCallIndex: [u8;2] = [53, 0];
	pub const SetReservesCallIndex: [u8;2] = [53, 33];
	pub Parameters: PricingParameters<u128> = PricingParameters {
		exchange_rate: FixedU128::from_rational(1, 400),
		fee_per_gas: gwei(20),
		rewards: Rewards { local: 1 * UNITS, remote: meth(1) },
		multiplier: FixedU128::from_rational(1, 1),
	};
```
