## Analysis

The external report's core broken invariant is: **a value-transfer call's result is not checked, so the caller advances protocol state (accounting) as if the transfer succeeded even when it silently failed.** Searching for this pattern across proof/queue/payout code, the strongest local analog is in `pallet-society`, where the internal reserve/unreserve helpers ignore the actual `Result` of `T::Currency::transfer` via `debug_assert!`, which is a no-op in production (release) builds.

### Title
Unchecked `Currency::transfer` result in `pallet-society::reserve_payout`/`unreserve_payout` allows payout accounting (`Pot`) to desync from actual balances - (File: `substrate/frame/society/src/lib.rs`)

### Summary
`reserve_payout` and `unreserve_payout` move funds between the society main account and the payouts sub-account while updating the `Pot` counter. The `Currency::transfer` result is captured but only checked with `debug_assert!(res.is_ok())`, which compiles away entirely in non-debug (production) builds, exactly mirroring the reported "`transferFrom` result not checked" pattern.

### Finding Description
`reserve_payout` unconditionally decrements the `Pot` storage value, then attempts to physically move `amount` from the society account to the payouts sub-account, discarding the transfer's `Result` behind a `debug_assert!`: [1](#0-0) 

`unreserve_payout` mirrors this in the opposite direction, again only `debug_assert!`-checking the result: [2](#0-1) 

Both helpers are called from non-fallible internal paths — `bump_payout` (recording a new pending payout) and `slash_payout` (discarding/reducing a pending payout) — which themselves return `()`/`BalanceOf<T>` rather than a `DispatchResult`, so there is no way for a transfer failure to propagate even in principle: [3](#0-2) 

By contrast, the actual member-facing `payout()` extrinsic correctly propagates the transfer error with `?`: [4](#0-3) 

If the underlying transfer in `reserve_payout`/`unreserve_payout` ever fails (e.g. `AllowDeath` preservation causing failure due to existential-deposit constraints, or the main/payouts account balance being out of sync with the `Pot`/`Payouts` bookkeeping for any other reason), the `Pot` counter and the `Payouts` map are still mutated as if the physical transfer succeeded, because the caller has no way to observe or react to failure. This is precisely the "settle only after execution succeeds" invariant being violated: bookkeeping advances unconditionally, while the actual currency movement is optional.

Notably, [`pr_12590`](prdoc/pr_12590.prdoc) — a prior audit fix for this exact pallet — already had to correct several related paths (`waive_repay`, `slash_payout`, `bump_payout`, `dissolve`) that left the payouts sub-account balance out of sync with `Payouts` records, and even added a `try_state` invariant asserting `payouts_account_balance == pending_payouts_total`. That fix did not address the `debug_assert!`-only handling in `reserve_payout`/`unreserve_payout` itself, which is the same class of accounting-vs-settlement desync the PR was trying to eliminate: [5](#0-4) 

### Impact Explanation
A desync between `Pot`/`Payouts` bookkeeping and the real balance of the society's payouts sub-account can cause: (1) legitimate members' `payout()` calls to fail indefinitely because the payouts sub-account lacks the physical funds its recorded `Payouts` entries promise (permanent fund lock for members), and/or (2) the `Pot` counter permanently under/over-counting the pot's real value, corrupting subsequent bid/vouch reward calculations that rely on `Pot`. This falls squarely in the "treasury or reward payouts... must conserve value and settle exactly once" and "payout state must only advance after execution and settlement succeed" impact categories.

### Likelihood Explanation
Under normal invariant-preserving conditions the transfer should always succeed because `amount <= Pot` and the main account should hold at least `Pot` of spendable balance, so this is primarily a defense-in-depth gap rather than a directly attacker-triggerable path in every case. However, it removes the last safety net for exactly the class of divergence that `pr_12590`'s own `try_state` check was added to detect, and any future edge case, migration bug, or interaction (e.g. donations, existential-deposit edge cases, or another reserve/unreserve caller) that causes the transfer to fail will silently and permanently corrupt the pot/payouts accounting in production builds with no error, no event, and no rollback.

### Recommendation
Replace `debug_assert!(res.is_ok())` in `reserve_payout` and `unreserve_payout` with proper error propagation (`?` or an explicit `Err` path that reverts the just-applied `Pot` mutation), and change their signatures (and callers `bump_payout`/`slash_payout`) to return `DispatchResult`/`Result` so failures are surfaced and handled instead of silently accepted, consistent with how `payout()` already checks its transfer.

### Proof of Concept
Not independently reproducible from static analysis alone: triggering the failure path requires first inducing a scenario where the main account's spendable balance is less than `Pot` (or the payouts account's balance is less than a recorded unreserve amount) — e.g. via a separate accounting bug, a race with `dissolve`, or an existential-deposit edge case — at which point calling `bump_payout`/`slash_payout` (via normal membership vouch/bid flows) will decrement/increment `Pot` while `debug_assert!` silently swallows the transfer failure in a release build. I was not able to fully verify all call sites that could produce the balance/`Pot` divergence precondition within the available search iterations; this should be validated dynamically (e.g. with a debug-assertions-disabled test harness) by a Devin session with full file/test access.

### Citations

**File:** substrate/frame/society/src/lib.rs (L1064-1073)
```rust
			let mut record = Payouts::<T, I>::get(&who);
			let block_number = T::BlockNumberProvider::current_block_number();
			if let Some((when, amount)) = record.payouts.first() {
				if when <= &block_number {
					record.paid = record.paid.checked_add(amount).ok_or(Overflow)?;
					T::Currency::transfer(&Self::payouts(), &who, *amount, AllowDeath)?;
					record.payouts.remove(0);
					Payouts::<T, I>::insert(&who, record);
					return Ok(());
				}
```

**File:** substrate/frame/society/src/lib.rs (L2133-2182)
```rust
	/// Bump the payout amount of `who`, to be unlocked at the given block number.
	///
	/// It is the caller's duty to ensure that `who` is already a member. This does nothing if `who`
	/// is not a member, if `value` is zero or if the payment cannot be recorded because the member
	/// already has too many pending payouts.
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

**File:** substrate/frame/society/src/lib.rs (L2184-2194)
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
```

**File:** substrate/frame/society/src/lib.rs (L2196-2206)
```rust
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

**File:** prdoc/pr_12590.prdoc (L1-15)
```text
title: 'pallet-society: return funds of discarded payouts to the pot'
doc:
- audience: Runtime Dev
  description: |-
    Funds backing pending payouts are moved into the payouts sub-account when scheduled, and must be returned whenever a payout is discarded. Four paths failed to do so, leaving balance stranded in the sub-account with no `Payouts` entry left to claim it:

    - `waive_repay` cleared the member's pending payouts without unreserving their backing funds
    - `slash_payout` deducted from pending payouts without unreserving the deducted amount
    - `bump_payout` reserved funds even when the payment was discarded because the member was already at `MaxPayouts` capacity
    - `dissolve` cleared all payout records without returning the payouts sub-account balance to the society account

    A `try_state` invariant now asserts that the payouts sub-account balance equals the total of all pending payouts. Deployments whose sub-account balance has already drifted — e.g. through the paths above, or through the `v0` migration, which carries payout records over without moving balances (the case of Kusama Asset Hub, for example) — can restore the invariant by adding the new unversioned, idempotent `pallet_society::migrations::ReconcilePayoutsAccount` migration to their runtime's migration tuple; until then, `try-runtime` checks will fail.
crates:
- name: pallet-society
  bump: minor
```
