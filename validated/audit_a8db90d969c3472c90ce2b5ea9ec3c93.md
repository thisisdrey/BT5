## Finding

### Title
Runtime panic on zero ETH/DOT exchange rate in Snowbridge fee calculation - ([File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs])

### Summary
The BadgerDAO report describes an oracle allowed to publish a `0` share price, which becomes a division denominator elsewhere and reverts via `SafeMath`, causing a DoS. The direct analog in this repository is `Pallet::calculate_fee` in the Snowbridge outbound-queue pallet, which divides by `params.exchange_rate` and unwraps the result with `.expect("exchange rate is not zero; qed")` instead of handling a zero rate gracefully.

### Finding Description
`calculate_fee` computes the local fee owed for delivering a message to Ethereum: [1](#0-0) 

```rust
let fee = FixedU128::from_inner(fee)
    .saturating_mul(params.multiplier)
    .checked_div(&params.exchange_rate)
    .expect("exchange rate is not zero; qed")
    .into_inner();
```

The comment assumes `params.exchange_rate` can never be zero, and the only enforcement of that invariant is `PricingParameters::validate()`: [2](#0-1) 

That `validate()` check is an opt-in helper, not something the compiler or the `Get<PricingParameters<Self::Balance>>` trait bound enforces at the type level. `T::PricingParameters` in the outbound-queue pallet's `Config` is a plain `Get` implementation (wired up in `bridge_to_ethereum_config.rs` in the bridge-hub runtimes), and it is only as safe as whatever code path sets the underlying value calls `validate()` before persisting it. If that value is ever persisted as zero — whether through a runtime upgrade, a misconfigured default, or any future write path to `PricingParameters` that doesn't call `validate()` — the `.expect()` in `calculate_fee` becomes a guaranteed **panic** rather than a recoverable `Err`.

This is structurally different from — and worse than — how the same class of "zero denominator" bug was already fixed elsewhere in this codebase. In `pallet-asset-conversion`, the exact same bug class (division by a value that can be zero) was hardened to return `None`/`Err` instead of panicking: [3](#0-2) 

And in `nomination-pools`, all point/balance ratio conversions are guarded with `is_zero()` checks before dividing: [4](#0-3) 

The outbound-queue pallet's fee calculation is the one place in this cluster of "price ratio" logic that still relies on an `.expect()` panic instead of a checked/graceful path — exactly the BadgerDAO pattern (0 price → division → unhandled failure) but escalated from a `revert` (caught, recoverable) to a Rust panic (unrecoverable trap during block execution).

### Impact Explanation
`calculate_fee` is on the message-send critical path for the outbound queue (used by `validate()` when any user or sibling parachain submits a message to be relayed to Ethereum). A panic inside this function, if triggered, would not be a caught `DispatchError` — it is a Rust panic during runtime execution. Depending on where in the call stack it's triggered (extrinsic validation vs. message-queue processing during `on_initialize`/`on_finalize`), this can degrade or halt Snowbridge message processing entirely, matching the "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
The `PricingParameters.exchange_rate` field is governance-controlled and currently gated by `validate()` at the point where it's normally set. This significantly limits attacker-reachability, since an unprivileged user cannot independently drive `exchange_rate` to zero. The primary defect is the missing type-level/defense-in-depth guarantee — the pallet trusts an external invariant enforced only by convention (`validate()` being called by every caller of the setter) rather than checking it at the point of use, unlike the `asset-conversion` and `nomination-pools` code which check locally right before dividing. I could not fully verify, within the available search budget, whether every write path to the governance-settable `PricingParameters` storage in `snowbridge-pallet-system` unconditionally calls `validate()` before persisting — this would need confirmation in a live session.

### Recommendation
Replace the `.expect("exchange rate is not zero; qed")` in `calculate_fee` with a `checked_div` that returns a graceful error/`None` (or saturates) instead of panicking, mirroring the pattern already used in `pallet-asset-conversion`'s `get_amount_out`/`quote_price_*` functions. This removes the reliance on an external, convention-only invariant and eliminates the panic path regardless of how `exchange_rate` reaches zero.

### Proof of Concept
1. Cause `PricingParameters.exchange_rate` (as returned by `T::PricingParameters::get()`) to be `FixedU128::zero()` — e.g., via any future/alternate write path to the governance-settable pricing storage that does not invoke `PricingParameters::validate()`, or via a misconfigured genesis/runtime-upgrade default.
2. Any user calls `send`/message submission that routes through `SendMessage::validate`, which calls `Pallet::calculate_fee(gas_used_at_most, params)`.
3. Execution reaches:
```rust
.checked_div(&params.exchange_rate)
.expect("exchange rate is not zero; qed")
```
4. `checked_div` returns `None` (division by `FixedU128::zero()`), and `.expect()` panics, aborting the extrinsic/message-processing execution rather than returning a typed error — stalling Snowbridge fee calculation for all subsequent message submissions until the parameter is corrected.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L382-387)
```rust
			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1396-1400)
```rust
			let reserve_out = T::HigherPrecisionBalance::from(*reserve_out);

			if reserve_in.is_zero() || reserve_out.is_zero() {
				return Err(Error::<T>::ZeroLiquidity);
			}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3503-3513)
```rust
	fn point_to_balance(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		points: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		if current_balance.is_zero() || current_points.is_zero() || points.is_zero() {
			// There is nothing to unbond
			return Zero::zero();
		}
```
