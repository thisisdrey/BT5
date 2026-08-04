### Title
Unchecked transfer result in `pallet-society::reserve_payout`/`unreserve_payout` silently desyncs the `Pot` from real payout-account balance - (File: `substrate/frame/society/src/lib.rs`)

### Summary
`Pallet::<T, I>::reserve_payout` and `Pallet::<T, I>::unreserve_payout` move funds between the society account and the `Payouts` sub-account and independently mutate the `Pot` storage value, but they never check the result of `T::Currency::transfer` — they only wrap it in `debug_assert!(res.is_ok())`, which compiles to a no-op in release/production builds. [1](#0-0) 

### Finding Description
`reserve_payout` decrements `Pot` unconditionally and then attempts a currency transfer from the society account to the `payouts()` sub-account; `unreserve_payout` does the symmetric operation in the other direction. In both functions the transfer's `Result` is discarded except for a `debug_assert!`:

```rust
fn reserve_payout(amount: BalanceOf<T, I>) {
    Pot::<T, I>::mutate(|pot| pot.saturating_reduce(amount));
    let res = T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath);
    debug_assert!(res.is_ok());
}

fn unreserve_payout(amount: BalanceOf<T, I>) {
    Pot::<T, I>::mutate(|pot| pot.saturating_accrue(amount));
    let res = T::Currency::transfer(&Self::payouts(), &Self::account_id(), amount, AllowDeath);
    debug_assert!(res.is_ok());
}
``` [2](#0-1) 

`debug_assert!` is compiled out entirely in release builds, which is how production runtimes are compiled. If the transfer fails for any reason (e.g. the recipient sub-account would go below `ExistentialDeposit` and gets no implicit-existence exemption, or the payouts account transiently lacks funds), the function silently returns `Ok(())`-equivalent control flow with:
- `Pot` already mutated (decremented in `reserve_payout`, incremented in `unreserve_payout`), and
- the `Payouts` map entry already updated by the caller (`bump_payout`, `slash_payout`, `waive_repay`, `kick_member`, etc.)

but the actual balance of the `payouts()` sub-account never moved. This is functionally the same defect flagged in the referenced report: a transfer's boolean/`Result` outcome is not validated, and failure is not handled (no revert, no error propagation).

The maintainers already recognized and partially fixed this class of bug for four other call sites in this very pallet — `waive_repay`, `slash_payout`, `bump_payout`-discard path, and `dissolve` — via PR that added `ReconcilePayoutsAccount` migration and a `do_try_state` invariant check (`payouts account balance must equal the total of pending payouts`). [3](#0-2) [4](#0-3) 

However, `reserve_payout`/`unreserve_payout` themselves — the two lowest-level primitives that actually perform the currency movement backing every `Payouts` record — were left untouched, still relying on `debug_assert!` only. Every code path that fixed the four other bugs (`bump_payout`, `slash_payout`, `waive_repay`, `dissolve`) ultimately calls into these same two unchecked-transfer functions, so the underlying accounting-desync primitive still exists in production.

### Impact Explanation
If the underlying `T::Currency::transfer` call ever fails in a release build (no panic, since `debug_assert!` is stripped), the pallet's core invariant — "the `payouts()` sub-account balance must equal the sum of all pending `Payouts` entries" — is silently broken:
- In `reserve_payout`: `Pot` is reduced, but the `payouts()` account is never funded. Subsequent calls to `Pallet::<T, I>::payout` for the affected member(s) will fail on `T::Currency::transfer(&Self::payouts(), &who, *amount, AllowDeath)` because the sub-account lacks the promised funds — permanently locking that member's recorded reward. `do_try_state` would only catch this off-chain in try-runtime testing, not in production.
- In `unreserve_payout` (used by `slash_payout`, `waive_repay`, `kick_member`): `Pot` is increased even though funds never actually returned to the society account, inflating the pot beyond real backing and letting future candidacy payouts be over-promised against funds that don't exist, again leading to failed claims for other members down the line.

This matches the required impact class of "permanent user-fund ... lock" caused by state (`Pot`/`Payouts`) advancing without the corresponding settlement (balance transfer) succeeding atomically, exactly the invariant class called out in the task ("payout state must only advance after ... settlement succeed atomically").

### Likelihood Explanation
No privileged actor, admin, governance, or validator collusion is required. Any unprivileged member/bidder who triggers `bump_payout` with an amount that causes the `payouts()` sub-account transfer to dip below `ExistentialDeposit` (e.g., a very small vouch tip or slash-remainder reserved when the sub-account currently holds zero balance) can trigger the failure path organically. The existing test suite for the pallet exercises payout amounts that are always well above ED in the mock, so this defect would not be caught by unit tests, and the `do_try_state`/migration tooling only detects the resulting drift after the fact rather than preventing it.

### Recommendation
Propagate the `Result` from `T::Currency::transfer` in both `reserve_payout` and `unreserve_payout` instead of using `debug_assert!`. Since both functions are currently `fn ... { .. }` (no `Result` return type) called from infallible call sites (`bump_payout`, `slash_payout`, etc.), refactor them to return `DispatchResult` and have callers propagate the error (reverting the `Pot` mutation on failure), or use `T::Currency::transfer` with a guard (e.g., only mutate `Pot` after a successful transfer) so no state advances unless the underlying settlement succeeds — mirroring the same fix pattern already applied for `waive_repay`, `slash_payout`, and `dissolve` in PR `12590`.

### Proof of Concept
1. Configure a runtime with a non-trivial `ExistentialDeposit` for the pallet's `Currency`.
2. Ensure `Payouts::<T, I>` for member `X` is empty and the `payouts()` sub-account balance is `0`.
3. Trigger `bump_payout(&X, when, value)` with `value < ExistentialDeposit` (e.g., via a small vouch tip in `reward_bidder`), which calls `reserve_payout(value)`.
4. In `reserve_payout`, `Pot` is reduced by `value`, then `T::Currency::transfer(&account_id(), &payouts(), value, AllowDeath)` fails with `TokenError::BelowMinimum` because the destination account doesn't exist and the deposit is below ED.
5. In a release build, `debug_assert!(res.is_ok())` is a no-op, so `reserve_payout` returns normally; `Payouts::<T, I>` now records `X` as owed `value`, but `payouts()` sub-account balance is still `0`.
6. When `X` calls `payout(origin)`, `T::Currency::transfer(&Self::payouts(), &X, value, AllowDeath)` fails because the sub-account is unfunded — `X`'s recorded reward is permanently unclaimable, and `Pot` accounting no longer matches on-chain reality (only detectable later via `do_try_state`/`ReconcilePayoutsAccount`, which requires an explicit governance-run migration to repair).

### Citations

**File:** substrate/frame/society/src/lib.rs (L2184-2206)
```rust
	/// Transfer some `amount` from the main account into the payouts account and reduce the Pot
	/// by this amount.
	fn reserve_payout(amount: BalanceOf<T, I>) {
		// Transfer payout from the Pot into the payouts account.
		Pot::<T, I>::mutate(|pot| pot.saturating_reduce(amount));

		// this should never fail since we ensure we can afford the payouts in a previous
		// block, but there's not much we can do to recover if it fails anyway.
		let res = T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath);
		debug_assert!(res.is_ok());
	}

	/// Transfer some `amount` from the main account into the payouts account and increase the Pot
	/// by this amount.
	fn unreserve_payout(amount: BalanceOf<T, I>) {
		// Transfer payout from the Pot into the payouts account.
		Pot::<T, I>::mutate(|pot| pot.saturating_accrue(amount));

		// this should never fail since we ensure we can afford the payouts in a previous
		// block, but there's not much we can do to recover if it fails anyway.
		let res = T::Currency::transfer(&Self::payouts(), &Self::account_id(), amount, AllowDeath);
		debug_assert!(res.is_ok());
	}
```

**File:** substrate/frame/society/src/lib.rs (L2231-2243)
```rust
	/// Ensure the correctness of the state of this pallet.
	///
	/// The balance of the payouts account must equal the total of all pending payouts recorded in
	/// `Payouts`, as funds are moved into the account when a payout is recorded and out of it when
	/// a payout is claimed or discarded.
	#[cfg(any(feature = "try-runtime", test))]
	pub fn do_try_state() -> Result<(), sp_runtime::TryRuntimeError> {
		frame_support::ensure!(
			T::Currency::free_balance(&Self::payouts()) == Self::pending_payouts_total(),
			"payouts account balance must equal the total of pending payouts",
		);
		Ok(())
	}
```

**File:** substrate/frame/society/src/migrations.rs (L111-120)
```rust
/// Reconcile the balance of the payouts account with the payouts recorded in storage.
///
/// The balance of the payouts account must equal the total of all pending payouts recorded in
/// `Payouts`. Deployments may have drifted from this invariant — e.g. through code which discarded
/// payout records without moving the balance backing them, or through
/// [`VersionUncheckedMigrateToV2`], which carries payout records over without funding the account.
/// This migration transfers the difference between the payouts account and the society account in
/// whichever direction restores the invariant. It is unversioned, idempotent and safe to keep in a
/// runtime's migration tuple across upgrades.
pub struct ReconcilePayoutsAccount<T, I = ()>(core::marker::PhantomData<(T, I)>);
```
