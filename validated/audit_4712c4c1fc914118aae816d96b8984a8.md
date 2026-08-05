Audit Report

## Title
Snowbridge outbound-queue can compute a zero remote delivery fee for non-zero pricing parameters, allowing underpriced message delivery - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
`Pallet::<T>::calculate_fee` in [1](#0-0)  computes the remote (Ethereum-side relayer compensation) component of a delivery fee via a chain of `U256`→`u128` downcast, `FixedU128` fixed-point multiplication/division, and a final truncating integer division in `convert_from_ether_decimals` at [2](#0-1) . This pipeline can truncate the remote fee to exactly `0` even when `fee_per_gas`, `reward`, and `multiplier` are all strictly positive, as directly demonstrated by the pallet's own regression test at [3](#0-2) .

## Finding Description
`calculate_fee` builds the remote fee from `calculate_remote_fee` (gas × fee_per_gas + reward, in wei precision), downcasts to `u128` via `defensive_unwrap_or(u128::MAX)`, reinterprets it as a `FixedU128` via `from_inner`, multiplies by `params.multiplier`, divides by `params.exchange_rate`, and then calls `convert_from_ether_decimals`, which performs `value.checked_div(10^(ETHER_DECIMALS - T::Decimals))` — a plain truncating integer division: [4](#0-3) [2](#0-1) .

With `ETHER_DECIMALS = 18` and native `Decimals` (10 for DOT, 12 for KSM), the divisor is `10^8` or `10^6`. Any wei-denominated result smaller than that divisor truncates to zero. The pallet's own test with `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`, `gas_used = 250000` confirms `fee.remote == 0` while `fee.local == 698000000` (non-zero) [3](#0-2) . This is called directly from `SendMessage::validate` in the fee-charging path used by every public message submission: [5](#0-4) . No code path checks that `fee.remote` is non-zero before returning the `Fee` to the caller, and no minimum/floor is enforced.

## Impact Explanation
This allows an unprivileged sender to submit messages to the outbound queue and pay `fee.remote == 0` for the relayer-compensation component of the delivery fee, even though governance-configured pricing parameters are non-zero and intended to compensate relayers for the real Ethereum gas they will spend. Because message admission into the queue (`do_process_message`) is only capacity-bounded via `MaxMessagesPerBlock`/`Yield` [6](#0-5)  rather than gated on a valid non-zero remote fee, senders can consume outbound-queue capacity, `MessageLeaves`/`Messages` storage, and merkle-commitment slots, and push messages that require relayer gas expenditure on Ethereum without paying for that expenditure. This matches the "public underpriced work" impact category for degrading bridge processing economics.

## Likelihood Explanation
The zero-fee condition is reachable with realistic, non-extreme parameter combinations — the pallet's own test uses `exchange_rate=1`, `fee_per_gas=1`, `reward=1`, `multiplier=1`, which are not pathological edge values. Any account able to call the public message-send path (e.g., via XCM export or `snowbridge-pallet-system::send`) benefits automatically whenever governance-set `PricingParameters` land in this degenerate low-fee-per-gas/low-reward region, requiring no privileged access, malicious relayer, or compromised validator — only a specific (but plausible) governance parameterization.

## Recommendation
In `calculate_fee`, explicitly verify that `fee.remote` (and `fee.local`) is non-zero whenever the underlying inputs (`fee_per_gas`, `rewards.remote`, `gas_used_at_most`) are non-zero, and reject the send (return an error) or apply a governance-defined minimum fee floor rather than silently returning `0`. Consider rounding up (ceiling division) instead of truncating in `convert_from_ether_decimals` and in the `FixedU128` division so genuinely small nonzero costs aren't floored away. Add a runtime invariant test sweeping realistic parameter ranges to catch this class of degeneracy rather than relying on one example.

## Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in [3](#0-2)  is a direct reproduction: calling `OutboundQueue::calculate_fee(250000, price_params)` with `exchange_rate=1/1`, `fee_per_gas=1`, `rewards={1,1}`, `multiplier=1/1` yields `fee.remote == 0` while `fee.local == 698000000`. A sender can invoke `SendMessage::validate`/`deliver` under a governance parameter set landing in this region to have a message queued, committed, and eventually relayed to Ethereum while paying zero remote (relayer-compensation) fee.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L307-313)
```rust
			// Yield if the maximum number of messages has been processed this block.
			// This ensures that the weight of `on_finalize` has a known maximum bound.
			ensure!(
				MessageLeaves::<T>::decode_len().unwrap_or(0) <
					T::MaxMessagesPerBlock::get() as usize,
				Yield
			);
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L414-418)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L59-60)
```rust
		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```
