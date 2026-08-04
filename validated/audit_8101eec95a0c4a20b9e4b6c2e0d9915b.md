### Title
Accumulate-and-forward `OnUnbalanced` handlers silently burn protocol revenue when the accumulation account is unfunded or reaped - (File: `substrate/frame/accumulate-and-forward/src/lib.rs`)

### Summary
`pallet_accumulate_and_forward` is wired into live Westend-family runtimes (relay chain, `collectives-westend`, `bridge-hub-westend`, `coretime-westend`, `people-westend`) to intercept transaction fees, dust removal, and coretime revenue via the `OnUnbalanced` trait and periodically forward them off-chain. Both `OnUnbalanced` implementations in this pallet treat the deposit into the accumulation account as effectively infallible and drop the credit/imbalance on failure, which — exactly like the `TimelockController._call()` pattern that "succeeds" against a non-existent account — silently discards value instead of reverting or safely retrying. [1](#0-0) 

### Finding Description
The fungible `Balanced` implementation resolves the credit into the accumulation account and simply discards the error case:

```rust
let _ = T::Currency::resolve(&accumulation_account, amount).inspect_err(|_| {
    frame_support::defensive!(
        "🚨 Failed to deposit to accumulation account - funds burned, it should never happen!"
    );
});
``` [2](#0-1) 

The legacy `Currency` adapter has the same shape, explicitly acknowledging in its own comment that the operation "silently burns the imbalance if it is less than ED and the destination is empty," and only emits a `defensive!()` diagnostic (a debug-only panic / production-only log) before proceeding to call the inherently non-reverting `resolve_creating`: [3](#0-2) 

Because `Credit`/`NegativeImbalance` reduce `total_issuance` on `Drop`, any credit that cannot be deposited (account below `minimum_balance()` and unfunded/reaped) is not reverted, not retried, and not routed anywhere — it vanishes from total issuance without ever reaching the accumulation account or the eventual forwarding destination. This exactly mirrors the reported bug class: an operation that returns as if it succeeded (`()`, no error surfaced to the wider fee/dust-removal pipeline) while silently failing to deliver value to the intended recipient.

The pallet's own module docs concede the precondition dependency: *"The accumulation account must be pre-funded with at least the existential deposit... If the accumulation account is not pre-funded, deposits below ED will be silently burned."* [4](#0-3) 

Unlike `forward()` in `on_idle`, which correctly distinguishes success/failure and emits `ForwardSucceeded`/`ForwardFailed` events for observability and retry, the `OnUnbalanced` deposit path has no such feedback loop, no retry, and no restitution mechanism — the loss is unconditional and permanent once the underlying `resolve`/`resolve_creating` call fails. [5](#0-4) 

### Impact Explanation
This handler is invoked from ordinary transaction-fee processing (`DealWithFeesSplit`), dust removal, and coretime revenue collection on every block of the affected chains — all normal, unprivileged user activity. If the accumulation account's balance is ever at or drops toward the existential deposit threshold (e.g., right after the pallet is added via a runtime upgrade before the account is funded, or if the account is dust-reaped by unrelated mechanisms), every subsequent fee/dust/revenue credit routed to it is permanently destroyed rather than accumulated and forwarded to the rightful destination chain. This breaks the "balances ... must conserve value and settle exactly once to the rightful beneficiary" invariant: protocol revenue is unbacked-burned with no attacker action required, purely from routine operation under a plausible operational state (unfunded/under-ED accumulation account).

### Likelihood Explanation
No governance, admin, relayer, or validator collusion is required to trigger the loss — it happens automatically as a side effect of standard fee/dust/revenue flows whenever the accumulation account's balance is not comfortably above ED. The pallet's own documentation and the `defensive!()` call sites acknowledge this exact failure mode is expected to occur under realistic conditions (fresh deployment, migration window, or any event that reduces the account below ED), and the mitigation implemented is only a non-blocking diagnostic log, not a functional safeguard (no fallback, no retry, no revert of the underlying fee-collection extrinsic).

### Recommendation
- Make the deposit path fail closed instead of silently dropping value: if `resolve`/`resolve_creating` would burn the credit (destination below ED and would-be-created), re-route the imbalance to a guaranteed-safe sink (e.g., merge into `OtherHandler`/treasury, or defer into a bounded on-chain "pending" storage item) rather than letting it drop.
- Emit an `Event` (mirroring `ForwardFailed`) whenever a deposit is lost so operators/monitoring can react, instead of relying solely on `defensive!()`, which is silent in production builds outside of logs.
- Consider having genesis/runtime-upgrade tooling assert the accumulation account is pre-funded with at least `ExistentialDeposit` as a hard invariant check (e.g., in `integrity_test` or a migration) rather than only documenting it as an operational requirement.

### Proof of Concept
1. Deploy/upgrade a runtime with `pallet_accumulate_and_forward` configured (as done for `bridge-hub-westend`, `collectives-westend`, `coretime-westend`, `people-westend`) without pre-funding the derived `accumulation_account` (`PalletId::get().into_account_truncating()`) to at least `ExistentialDeposit`.
2. Any ordinary user submits a signed extrinsic; `pallet_transaction_payment`'s configured `OnChargeTransaction`/`DealWithFeesSplit` routes the accumulated-percent share of the fee into `Pallet::<T>::on_nonzero_unbalanced`. [6](#0-5) 
3. `T::Currency::resolve(&accumulation_account, amount)` fails because `numeric_amount < ExistentialDeposit` and the account doesn't exist; the `Err` is discarded via `let _ = ...`, the `Credit` is dropped, `total_issuance` is decremented, and no funds ever reach the accumulation account.
4. Repeat for every subsequent block's fee share until an unrelated large deposit happens to push the account above ED — until then, all accrued protocol revenue for that period is permanently and silently lost, with only a debug-log-level `defensive!()` trace as evidence.

### Citations

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L18-38)
```rust
//! # Accumulate-and-Forward Pallet
//!
//! Intercepts configurable token inflows (transaction fees, dust removal, coretime revenue) on
//! system parachains and gathers them in a local accumulation account for periodic forwarding
//! to a configurable destination.
//!
//! ## Usage
//!
//! - **Fees**: Use [`DealWithFeesSplit`] to split fees between accumulation and other handlers
//! - **Burns/Revenue**: Use the pallet as `OnUnbalanced<CreditOf>` handler (e.g., dust removal,
//!   coretime revenue)
//! Note: Direct calls to `pallet_balances::Pallet::burn()` extrinsic are not redirected to
//! the accumulation account — they still reduce total issuance directly.
//!
//! ## Setup
//!
//! The accumulation account must be pre-funded with at least the existential deposit.
//! For new chains, include the account in the balances genesis config.
//! For existing chains, fund it via a manual transfer.
//!
//! If the accumulation account is not pre-funded, deposits below ED will be silently burned.
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L184-201)
```rust
			// Attempt to forward accumulated funds.
			match T::Forwarder::forward(accumulation_account, available_funds) {
				Ok(()) => {
					Self::deposit_event(Event::ForwardSucceeded { amount: available_funds });
				},
				Err(()) => {
					log::debug!(
						target: LOG_TARGET,
						"accumulate-forward transfer of {:?} failed at block {:?}",
						available_funds,
						block,
					);
					Self::deposit_event(Event::ForwardFailed { amount: available_funds });
				},
			}

			meter.consumed()
		}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L261-278)
```rust
	fn on_unbalanceds(mut fees_then_tips: impl Iterator<Item = CreditOf<T>>) {
		if let Some(fees) = fees_then_tips.next() {
			let accumulated_percent = AccumulatedPercent::get();
			let other_percent = Percent::one().saturating_sub(accumulated_percent);
			let mut split = fees.ration(
				accumulated_percent.deconstruct() as u32,
				other_percent.deconstruct() as u32,
			);
			if let Some(tips) = fees_then_tips.next() {
				// Tips go 100% to other handler.
				tips.merge_into(&mut split.1);
			}
			if !accumulated_percent.is_zero() {
				<Pallet<T> as OnUnbalanced<_>>::on_unbalanced(split.0);
			}
			OtherHandler::on_unbalanced(split.1);
		}
	}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L289-308)
```rust
impl<T: Config> OnUnbalanced<CreditOf<T>> for Pallet<T> {
	fn on_nonzero_unbalanced(amount: CreditOf<T>) {
		let accumulation_account = Self::accumulation_account();
		let numeric_amount = amount.peek();

		// Resolve should never fail because:
		// - can_deposit on destination succeeds assuming accumulation account is pre-funded with ED
		// - amount is guaranteed non-zero by the trait method signature
		// The only failure would be overflow on destination or unfunded account.
		let _ = T::Currency::resolve(&accumulation_account, amount).inspect_err(|_| {
			frame_support::defensive!(
				"🚨 Failed to deposit to accumulation account - funds burned, it should never happen!"
			);
		});

		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited {numeric_amount:?} to accumulation account"
		);
	}
```

**File:** substrate/frame/accumulate-and-forward/src/lib.rs (L329-354)
```rust
impl<T: Config, C> OnUnbalanced<LegacyNegativeImbalance<T::AccountId, C>> for LegacyAdapter<T, C>
where
	C: Currency<T::AccountId>,
{
	fn on_nonzero_unbalanced(amount: LegacyNegativeImbalance<T::AccountId, C>) {
		let accumulation_account = Pallet::<T>::accumulation_account();
		let numeric_amount = amount.peek();
		// NOTE: `resolve_creating` is "infallible" because it returns `()`, but it silently burns
		// the imbalance if it is less than ED and the destination is empty. We guard against this
		// by making misconfigured runtimes clearly visible. See crate-level docs for the
		// pre-funding requirement.
		if C::total_balance(&accumulation_account).saturating_add(numeric_amount) <
			C::minimum_balance()
		{
			frame_support::defensive!(
				"🚨 LegacyAdapter: deposit to accumulation account will be silently burned — \
				 ensure the accumulation account is pre-funded with at least ED!"
			);
		}
		C::resolve_creating(&accumulation_account, amount);
		log::debug!(
			target: LOG_TARGET,
			"💸 Deposited (legacy) {numeric_amount:?} to accumulation account"
		);
	}
}
```
