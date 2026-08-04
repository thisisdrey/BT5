## Title
Snowbridge outbound message delivery fee rounds down to zero, letting users pay nothing for Ethereum-bound messages while relayers/reward pot bear the cost - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

## Summary
`Pallet::calculate_fee` computes the fee a sender must pay to have a message committed and relayed to Ethereum. The remote-fee component is converted through `FixedU128` arithmetic and then truncated by integer division in `convert_from_ether_decimals`. With small but non-zero pricing parameters (`fee_per_gas`, `reward`, `exchange_rate`, `multiplier`), the computed remote fee can be truncated to `0`, exactly the "positive input produces zero output" pattern described in the external report (division/precision loss before the value that should compensate the payee is realized). This is directly demonstrated by the pallet's own test, whose comment explicitly flags it as a defect.

## Finding Description
`calculate_fee` (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`) does:
1. `calculate_remote_fee` → `fee_per_gas * gas_used_at_most + reward` (in wei/`U256`).
2. Cast to `u128`, wrap in `FixedU128`, multiply by `params.multiplier`, divide by `params.exchange_rate`, take `.into_inner()`.
3. `convert_from_ether_decimals` divides the 18-decimal wei value by `10^(18 - T::Decimals)` (e.g. `10^8` for a 10-decimal native currency) using plain integer `checked_div`, which truncates any remainder to zero: [1](#0-0) 

This final truncating division is applied *after* the fee has already been scaled/rounded by the exchange-rate division inside `FixedU128`, so a legitimate, non-zero `fee_per_gas`/`reward` combination can still yield an inner wei value smaller than the decimal-conversion denominator, collapsing the final `remote` fee to `0`. The pallet's own regression test proves this: [2](#0-1) 

The test comment ("Though none zero pricing params the remote fee calculated here is invalid which should be avoided") acknowledges the defect but no guard is present to reject or floor a zero remote fee. `validate()` (`bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs:41-74`) simply calls `calculate_fee` and returns whatever `Fee` results — including `remote == 0` — with no minimum-fee assertion, and the message ticket is unconditionally accepted for enqueue/delivery.

## Impact Explanation
The `remote` component of the fee is meant to reimburse the gas relayers spend executing the command on Ethereum plus their reward (`bridges/snowbridge/pallets/outbound-queue/src/lib.rs:60-67,72-78`). If pricing parameters (which are periodically set by governance and can legitimately include small values, e.g. a lower `fee_per_gas`/`reward` after a market move, or intentionally-set edge values) combine such that the wei amount is below `10^(18-Decimals)`, any user can submit a governance-approved-priced message that is charged `0` for the remote leg while the relayer still incurs real ETH gas cost to execute it on Ethereum. This is "public underpriced work that ... stalls bridge processing": senders get subsidized/free Ethereum execution at the expense of relayer economics, degrading the incentive to relay and potentially starving/backlogging the bridge queue, without any privileged actor, malicious relayer, or governance abuse involved — a normal user triggers it purely by paying whatever fee the pallet itself computes as legitimate.

## Likelihood Explanation
Likelihood is moderate-to-high given the current constant pricing parameters: `T::Decimals` is enforced to be 10 or 12 (`integrity_test`, `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:259-262`), meaning the truncating denominator is `10^8` or `10^6`. Any dip in `fee_per_gas`/`reward` or configuration drift (which is explicitly described as periodically, manually updated by governance in the module docs) can push the wei-scaled value below this denominator for low-gas commands, and the bug is entirely triggerable by any account calling the ordinary message-send path — no privileged access needed, and it is already reproduced by the pallet's own unit test with realistic-looking parameters (`exchange_rate = 1`, `fee_per_gas = 1`, `reward = 1`).

## Recommendation
- In `calculate_fee` / `convert_from_ether_decimals`, reject (or round up, or apply a floor of 1) rather than silently truncating the remote fee to zero when the input value is non-zero: compare pre- and post-division values and error/saturate to a minimum charge instead of allowing `remote == 0` for non-zero pricing parameters.
- Add an explicit `ensure!(fee.remote > 0 || pricing_params_are_zero, Error::<T>::FeeTooLow)`-style guard in `validate()` before returning the `Ticket`, so no message can be queued for remote execution with an under-priced (zero) remote fee while relayer-facing gas/reward parameters are non-zero.
- Perform the multiply-then-divide chain in a single higher-precision operation (e.g. `u256`) to avoid compounding rounding losses across `FixedU128` division and the final decimal-conversion division.

## Proof of Concept
The existing unit test in the repository already demonstrates the flaw end-to-end: [2](#0-1) 

With `exchange_rate = FixedU128::from_rational(1,1)`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, and `gas_used = 250000`, `calculate_remote_fee` yields `250000*1+1 = 250001` wei, which after `FixedU128` multiply/divide and `convert_from_ether_decimals` (dividing by `10^8` for 10-decimal `Test::Decimals`) truncates to `fee.remote == 0`, while `fee.local` remains `698000000` — i.e. the sender pays only the local weight fee and nothing toward the relayer's Ethereum gas/reward, even though `fee_per_gas` and `reward` were both explicitly non-zero.

### Citations

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
