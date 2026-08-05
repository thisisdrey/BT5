## Title
Snowbridge outbound-queue can compute a zero remote delivery fee for non-zero pricing parameters, allowing underpriced message delivery - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

### Summary
The external report's core defect is a fee/cost calculation that can silently resolve to zero (or negative, clamped) despite the underlying economic parameters being non-zero, so a caller-triggered action proceeds "for free" past a guard that was meant to make it economically infeasible. The Snowbridge outbound-queue pallet's `calculate_fee`/`calculate_remote_fee`/`convert_from_ether_decimals` chain has the same class of defect: a fixed-point computation involving `checked_div`, integer truncation via `into_inner()`, and decimal-scale division can produce `fee.remote == 0` even though `fee_per_gas`, `reward`, and `multiplier` are all non-zero, as demonstrated by the pallet's own regression test.

### Finding Description
`Pallet::<T>::calculate_fee` (bridges/snowbridge/pallets/outbound-queue/src/lib.rs, `calculate_fee`/`calculate_remote_fee`/`convert_from_ether_decimals`) computes the delivery fee owed by a sender for having a message relayed to Ethereum: [1](#0-0) 

The remote fee is computed in wei-like `U256` precision, then downcast via `fee.try_into().defensive_unwrap_or(u128::MAX)`, multiplied by `params.multiplier`, divided by `params.exchange_rate` via fixed-point arithmetic, and finally passed through `convert_from_ether_decimals`, which does an integer division by `10^(ETHER_DECIMALS - T::Decimals)`: [2](#0-1) 

Because this pipeline mixes a `U256`→`u128` downcast, `FixedU128::from_inner` reinterpretation, and a final truncating integer division by a large decimals-based divisor, the final local-currency `remote` fee can round down to `0` even when `fee_per_gas`, `reward`, and `multiplier` are all strictly positive. This is directly confirmed by the pallet's own test: [3](#0-2) 

The comment in the test itself states: "Though none zero pricing params the remote fee calculated here is invalid which should be avoided" — i.e., the pallet authors are already aware the invariant "non-zero priced parameters ⇒ non-zero computed fee" can be violated, but no code path enforces a minimum/non-zero floor or rejects a zero result before the fee is used to charge the sender and accept the message for remote delivery. This mirrors the `in3-server` bug exactly: a downstream cost formula built from several sub-calculations can degrade to zero (or near-zero) even when the inputs that are supposed to make the action costly are non-zero, and nothing gates on that outcome before the "expensive" action (message enqueue/commit, analogous to conviction) proceeds.

Unlike the in3-server case, this fee is charged to *the sender* rather than being a guard against convicting a node, but the underlying invariant break is the same: the code assumes a calculated economic value can never be pathologically zero/small, and does not verify that assumption before proceeding with state-changing, chain-resource-consuming work (queuing a message that must be committed, weighed, and relayed to Ethereum).

### Impact Explanation
If `calculate_fee`'s remote component resolves to `0` (or to a value far below the actual expected relayer compensation/refund) for certain combinations of `PricingParameters` (`exchange_rate`, `fee_per_gas`, `rewards.remote`, `multiplier`), a user can submit messages to be committed and dispatched to Ethereum while paying negligible-to-zero remote fee. Because message processing in `do_process_message` is also bounded to `T::MaxMessagesPerBlock` per block via a `Yield` on overflow rather than an economic price signal, this creates a path for spam messages to consume Snowbridge outbound-queue capacity, merkle-commitment/message-leaf slots, and downstream relayer/Ethereum gas incentive budget without paying the fee designed to compensate that gas — i.e., "public underpriced work that degrades … stalls bridge processing" per the impact gate.

### Likelihood Explanation
Triggering the zero-fee condition depends on the specific combination of governance-set `PricingParameters` (exchange rate, fee-per-gas, multiplier) relative to `gas_used_at_most` for a given command, and on `T::Decimals`/`ETHER_DECIMALS` scaling. The pallet's own test demonstrates it is reachable with realistic-looking parameters (`exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1`), i.e., not a pathological corner case requiring extreme values, but a routine low-fee-per-gas / low-reward configuration. Any unprivileged account able to call `send`/`validate`+`deliver` for the outbound queue can benefit from a degenerate parameterization without needing a malicious relayer, validator, or governance actor.

### Recommendation
- In `calculate_fee`, explicitly check that `fee.remote` (and `fee.local`) is non-zero whenever the underlying pricing inputs (`fee_per_gas`, `rewards.remote`, `gas_used_at_most`) are non-zero, and reject/floor the fee (e.g., return an error or enforce a governance-defined minimum fee) rather than silently returning `0`.
- Perform the fixed-point division and decimal conversion with higher intermediate precision (or round up rather than truncate) so genuinely small-but-nonzero costs are not floored to zero.
- Add a runtime invariant/assertion (e.g., in `do_try_state` or in `calculate_fee` itself) enforcing "non-zero priced parameters ⇒ non-zero fee," and add benchmarmd/property tests sweeping the parameter space rather than relying on a single fixed example.
- Ensure whatever charges the sender (fee-payment logic that consumes `Fee<T::Balance>`) actively rejects delivery when the computed fee is `0` unless governance has explicitly configured a zero-fee allowance.

### Proof of Concept
The existing pallet test is itself a proof of concept for the broken invariant: [3](#0-2) 

With `exchange_rate = FixedU128::from_rational(1,1)`, `fee_per_gas = 1`, `rewards = { local: 1, remote: 1 }`, `multiplier = FixedU128::from_rational(1,1)`, and `gas_used = 250000`, `OutboundQueue::calculate_fee(...)` returns `fee.remote == 0` even though every pricing input is strictly positive. Any sender able to construct/send a message under a governance parameter set that lands in this degenerate region gets the remote component of delivery priced at zero, letting them push messages through the outbound queue (consuming `MessageLeaves`, `Messages` storage, and eventual Ethereum-side relayer gas budget) without paying for the corresponding relayer compensation.

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
