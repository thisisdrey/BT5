Audit Report

## Title
Unchecked transfer result in `pallet-society::reserve_payout`/`unreserve_payout` silently desyncs the `Pot` from real payout-account balance - (File: `substrate/frame/society/src/lib.rs`)

## Summary
`Pallet::<T, I>::reserve_payout` and `Pallet::<T, I>::unreserve_payout` unconditionally mutate the `Pot` storage value and then attempt a `T::Currency::transfer` between the society account and the `payouts()` sub-account, discarding the transfer's `Result` behind a `debug_assert!`, which is compiled out in release builds. [1](#0-0)  If the transfer fails (e.g. destination-account creation below `ExistentialDeposit`), `Pot` and `Payouts` records advance while the real balance movement never happens, permanently desyncing recorded payouts from the funds meant to back them.

## Finding Description
`reserve_payout` reduces `Pot` first, then calls `T::Currency::transfer(&Self::account_id(), &Self::payouts(), amount, AllowDeath)`, and `unreserve_payout` does the mirror operation, both only checking the result via `debug_assert!(res.is_ok())`. [2](#0-1)  `debug_assert!` is a no-op in release builds, so any transfer failure (most plausibly the balances-pallet rule that a destination account cannot be created with a balance below `ExistentialDeposit`, independent of `AllowDeath`/`KeepAlive`) is silently swallowed while the `Pot` mutation and the caller's `Payouts` mutation have already been committed.

`reserve_payout` is invoked from `bump_payout`, which is called during candidate/vouch reward processing with attacker-influenceable amounts (e.g. a very small vouch tip), after `Payouts` has already been updated: [3](#0-2)  `unreserve_payout` is invoked from `slash_payout` after `Payouts` has already been rewritten: [4](#0-3) 

The maintainers already acknowledged this exact defect class for other call sites (`waive_repay`, `slash_payout` discard path, `dissolve`) and added a `do_try_state` invariant plus a `ReconcilePayoutsAccount` repair migration, explicitly noting that "code which discarded payout records without moving the balance backing them" caused drift: [5](#0-4)  and [6](#0-5)  But the two lowest-level primitives that actually perform the currency movement — `reserve_payout` and `unreserve_payout` — were left unfixed, still relying solely on `debug_assert!`, so the underlying accounting-desync primitive remains reachable in production (non-debug) builds.

## Impact Explanation
This breaks the pallet's core invariant that the `payouts()` sub-account balance must equal the sum of all pending `Payouts` entries, as directly documented by the pallet's own `do_try_state` check. [6](#0-5)  A failed `reserve_payout` leaves a member's `Payouts` entry recorded without the backing funds in the `payouts()` account, causing that member's later `payout` extrinsic to fail with an insufficient-balance error and permanently locking their recorded reward — a real fund-lock impact. A failed `unreserve_payout` inflates `Pot` without actually returning funds, over-promising future payouts against non-existent backing. This matches the "payout state must only advance after ... settlement succeed atomically" pivot and the "permanent user-fund ... lock" impact category.

## Likelihood Explanation
No privileged actor is required. `reserve_payout` is reachable via `bump_payout`, which is called from vouch-tip processing during the periodic candidate rotation with amounts influenced by an ordinary bidder/voucher (e.g., a tip amount below `ExistentialDeposit`). [7](#0-6)  Because destination-account creation in `pallet_balances` requires a deposit at or above `ExistentialDeposit` regardless of the `AllowDeath` flag, a small enough amount to a never-before-funded `payouts()` account fails organically. This is not caught by the existing mock-based test suite, which uses payout amounts well above ED, and only the offline `do_try_state`/governance-run `ReconcilePayoutsAccount` migration detects the resulting drift after the fact.

## Recommendation
Change `reserve_payout` and `unreserve_payout` to return a `DispatchResult` (or equivalent fallible signature) and propagate the `T::Currency::transfer` error to callers (`bump_payout`, `slash_payout`, etc.), only committing the `Pot`/`Payouts` mutation after the transfer succeeds, or reorder operations so `Pot` is mutated only once the transfer is confirmed successful — mirroring the fix pattern already applied to `waive_repay`, `slash_payout`'s other paths, and `dissolve`.

## Proof of Concept
1. Configure a runtime with `ExistentialDeposit` > 0 for the pallet's `Currency`, and ensure the `payouts()` sub-account currently has zero balance and does not exist.
2. Trigger a vouch-tip reward flow (via `reward_bidder`/rotation logic) that calls `Self::bump_payout(&voucher, maturity, tip.min(value))` with a tip amount below `ExistentialDeposit`. [7](#0-6) 
3. Inside `bump_payout`, `Payouts` is updated and `reserve_payout(value)` is called; `Pot` is reduced, then `T::Currency::transfer(&account_id(), &payouts(), value, AllowDeath)` fails because the destination account can't be created below ED. [8](#0-7) 
4. In a release build, `debug_assert!(res.is_ok())` is stripped, so execution proceeds normally; `Payouts` now records the voucher as owed `value`, but the `payouts()` sub-account balance is still `0`.
5. When the voucher later calls the `payout` extrinsic, the transfer from `payouts()` to the voucher fails for lack of funds, permanently locking the recorded reward; run `do_try_state` to confirm the invariant `T::Currency::free_balance(&Self::payouts()) == Self::pending_payouts_total()` is violated. [9](#0-8)

### Citations

**File:** substrate/frame/society/src/lib.rs (L2110-2120)
```rust
			BidKind::Vouch(voucher, tip) => {
				// Check that the voucher is still vouching, else some other logic may have removed
				// their status.
				if let Some(mut record) = Members::<T, I>::get(&voucher) {
					if let Some(VouchingStatus::Vouching) = record.vouching {
						// In the case that a vouched-for bid is accepted we unset the
						// vouching status and transfer the tip over to the voucher.
						record.vouching = None;
						Self::bump_payout(&voucher, maturity, tip.min(value));
						Members::<T, I>::insert(&voucher, record);
						value.saturating_sub(tip)
```

**File:** substrate/frame/society/src/lib.rs (L2138-2159)
```rust
	fn bump_payout(who: &T::AccountId, when: BlockNumberFor<T, I>, value: BalanceOf<T, I>) {
		if value.is_zero() {
			return;
		}
		if let Some(MemberRecord { rank: 0, .. }) = Members::<T, I>::get(who) {
			let recorded = Payouts::<T, I>::mutate(who, |record| {
				// Members of rank 1 never get payouts.
				match record.payouts.binary_search_by_key(&when, |x| x.0) {
					Ok(index) => {
						record.payouts[index].1.saturating_accrue(value);
						true
					},
					// A member with too many pending payouts forfeits the payment.
					Err(index) => record.payouts.try_insert(index, (when, value)).is_ok(),
				}
			});
			// Only reserve funds for payments which have been recorded.
			if recorded {
				Self::reserve_payout(value);
			}
		}
	}
```

**File:** substrate/frame/society/src/lib.rs (L2161-2182)
```rust
	/// Attempt to slash the payout of some member, returning the funds reserved for the deducted
	/// amount to the pot. Return the total amount that was deducted.
	fn slash_payout(who: &T::AccountId, value: BalanceOf<T, I>) -> BalanceOf<T, I> {
		let mut record = Payouts::<T, I>::get(who);
		let mut rest = value;
		while !record.payouts.is_empty() {
			if let Some(new_rest) = rest.checked_sub(&record.payouts[0].1) {
				// not yet totally slashed after this one; drop it completely.
				rest = new_rest;
				record.payouts.remove(0);
			} else {
				// whole slash is accounted for.
				record.payouts[0].1.saturating_reduce(rest);
				rest = Zero::zero();
				break;
			}
		}
		Payouts::<T, I>::insert(who, record);
		let slashed = value - rest;
		Self::unreserve_payout(slashed);
		slashed
	}
```

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
