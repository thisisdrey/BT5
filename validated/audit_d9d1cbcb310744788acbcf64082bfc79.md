# #Vulnerability found for this question

### Title
`claim_swap` unconditionally deletes the `PendingSwap` record even when the underlying claim only partially succeeds, permanently stranding the source's reserved funds - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
`claim_swap` calls `swap.action.claim(&swap.source, &target)` and then removes the `PendingSwaps` entry **unconditionally**, regardless of what `claim()` returned [1](#0-0) . For the shipped `BalanceSwapAction`, `claim()` is implemented as `C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()` [2](#0-1) . Because `Currency::repatriate_reserved` performs a `BestEffort`/`Polite` transfer of reserved funds (`Self::do_transfer_reserved(slashed, beneficiary, value, BestEffort, Polite, status)`), it can move **less than the requested `value`** (including zero) while still returning `Ok(remaining)` [3](#0-2) . `.is_ok()` therefore evaluates to `true` even when only a fraction of the swap amount was actually delivered to the target, and the pallet still treats the swap as fully claimed, removing the `PendingSwap` entry and emitting `SwapClaimed { success: true }`.

### Finding Description
Once the entry is removed, the only remaining path to unreserve the source's leftover reserved balance is `cancel_swap`, which requires the entry to still exist (`PendingSwaps::get(...).ok_or(Error::<T>::NotExist)?`) [4](#0-3) . Since the entry was already deleted by `claim_swap`, `cancel_swap` can never be called again for that swap. Any portion of the source's balance that was reserved via `action.reserve(&source)` in `create_swap` but not actually transferred by the `BestEffort`/`Polite` repatriation therefore remains permanently reserved with no dispatchable path (public or privileged) to release it.

This is triggerable whenever the `Polite` liveness constraint prevents `do_transfer_reserved` from moving the full amount — e.g., the source account has non-reserved `frozen`/locked balance (staking bonds, vesting, governance locks) that the `Polite` check refuses to breach, or the source's total balance would otherwise be pushed below the "frozen" threshold. The target does not need to cooperate maliciously; any `target` account (including one fully controlled by the attacker as counterparty in `create_swap`) can drive the source into this state simply by revealing the proof and calling `claim_swap` normally — the pallet itself masks the partial failure and removes state that should have remained recoverable.

### Impact Explanation
This produces a permanent, unrecoverable lock of the swap source's on-chain funds (their reserved balance can never be unreserved again), which matches the "Permanent production fund lock" impact called out in the Required Impacts. Additionally, `SwapClaimed { success: true }` misrepresents settlement (the swap is reported as fully successful even when the transferred amount is less than intended), which is a wrong-amount settlement bug.

### Likelihood Explanation
No privileged access is required. Both parties to a `create_swap`/`claim_swap` pair are ordinary signed accounts; the source only needs to have locked/frozen balance (a very common condition for real accounts staking, vesting, or under governance locks) at the time of `claim_swap`. This can occur unintentionally in normal use, and can also be deliberately engineered by a counterparty who knows the source's lock state to strand the source's reserved funds.

### Recommendation
`claim_swap` should not unconditionally remove the `PendingSwap` entry when `action.claim()` reports (or effectively achieves) only partial success. Options:
- Change `SwapAction::claim` to return the actually-transferred amount (or a `Result` capturing partial success) instead of a bare `bool`, and only remove the storage entry (and truly finalize the swap) when the full committed value was moved; otherwise leave the reduced-value entry in place so `cancel_swap` (or a retried `claim_swap`) can still recover the remainder.
- Alternatively, use `Exact`/non-`BestEffort` precision in `repatriate_reserved` so that a partial transfer is a hard error, causing `claim_swap` to fail atomically (leaving the entry, and the reserved funds, intact) rather than silently reporting success.

### Proof of Concept
1. Source `A` calls `create_swap(target = B, hashed_proof, action = BalanceSwapAction::new(100), duration)`. This reserves `100` from `A`'s free balance.
2. Before `B` claims, `A`'s account accumulates a `frozen`/lock amount (e.g. via staking bond or vesting) such that a `Polite` transfer of the full `100` out of reserved would push `A`'s total balance below the frozen threshold.
3. `B` calls `claim_swap(proof, action)`. `repatriate_reserved` executes with `BestEffort`+`Polite`, moves less than `100` to `B` (possibly `0`), and returns `Ok(remaining)`, so `claim()` returns `true`.
4. `claim_swap` removes the `PendingSwap` entry and emits `SwapClaimed { success: true }` even though less than `100` was delivered.
5. `A` can no longer call `cancel_swap` (entry no longer exists → `Error::NotExist`), so the un-transferred remainder of the original `100` reservation on `A` is permanently stuck in `reserved` balance with no dispatchable call able to release it.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-155)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L311-313)
```rust
			let succeeded = swap.action.claim(&swap.source, &target);

			PendingSwaps::<T>::remove(target.clone(), hashed_proof);
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L339-340)
```rust
			let swap = PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?;
			ensure!(swap.source == source, Error::<T>::SourceMismatch);
```

**File:** substrate/frame/balances/src/impl_currency.rs (L725-734)
```rust
	fn repatriate_reserved(
		slashed: &T::AccountId,
		beneficiary: &T::AccountId,
		value: Self::Balance,
		status: Status,
	) -> Result<Self::Balance, DispatchError> {
		let actual =
			Self::do_transfer_reserved(slashed, beneficiary, value, BestEffort, Polite, status)?;
		Ok(value.saturating_sub(actual))
	}
```
