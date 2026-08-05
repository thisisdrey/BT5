Audit Report

## Title
Atomic-swap `claim_swap` permanently locks reserved funds when `SwapAction::claim` fails, by deleting the swap record unconditionally - (File: `substrate/frame/atomic-swap/src/lib.rs`)

## Summary
`pallet_atomic_swap::claim_swap` calls `swap.action.claim(&swap.source, &target)`, captures the resulting `bool` into `succeeded`, but unconditionally executes `PendingSwaps::<T>::remove(...)` and returns `Ok(())` regardless of that boolean. When `claim` fails (e.g. `repatriate_reserved` errors because the target account is below the existential deposit or was reaped), the funds reserved by `source` at `create_swap` time become permanently unreachable, since the only recovery path, `cancel_swap`, requires the now-deleted `PendingSwaps` entry.

## Finding Description
`create_swap` reserves the source's resources via `action.reserve(&source)?` [1](#0-0) . `claim_swap` then performs settlement and deletes the pending swap unconditionally, independent of whether `claim` succeeded: [2](#0-1) . The `SwapAction` trait explicitly documents `claim` as a fallible, first-class outcome ("Returns whether the claim succeeds") rather than an unreachable error case: [3](#0-2) .

For the built-in `BalanceSwapAction`, `claim` is implemented by calling `repatriate_reserved`, whose failure is silently converted to `false` via `.is_ok()`: [4](#0-3) . `repatriate_reserved` can fail for ordinary reasons (target account non-existent/below existential deposit, or reaped between `create_swap` and `claim_swap`), with no attacker sophistication required.

Once `claim` returns `false`, the `PendingSwaps` entry is still removed at line 313, so the only other code path capable of returning funds to `source` — `cancel_swap`, which looks up the same storage entry and calls `swap.action.cancel(&swap.source)` (which in turn calls `C::unreserve`) — permanently fails with `Error::<T>::NotExist`: [5](#0-4) , [6](#0-5) . The reserved balance on `source` remains reserved indefinitely, neither transferred to `target` nor returned to `source`.

## Impact Explanation
This is a permanent, unprivileged user-fund lock: any target account for which the settlement transfer legitimately fails (unfunded/reaped account, or any custom `SwapAction::claim` implementation that can genuinely fail) causes the source's escrowed funds to become unrecoverable, satisfying the "permanent user-fund ... lock" impact criterion in the Polkadot SDK Impact Gate.

## Likelihood Explanation
No privileged role or adversarial coordination is required. The `target` need only be an account for which `repatriate_reserved` fails (e.g., balance below existential deposit) — this can occur accidentally, or be intentionally exploited by a target who calls `claim_swap` to trigger removal of the `PendingSwaps` entry while gaining nothing themselves, at zero cost, permanently denying `source` access to their reserved funds.

## Recommendation
`claim_swap` should not unconditionally remove `PendingSwaps` when `claim` reports failure. Either only remove the entry and emit `SwapClaimed{success:true}` on `claim() == true` (leaving the entry available for `cancel_swap` after `end_block` on failure), or change `SwapAction::claim` to return a `DispatchResult`, propagating the error to abort the extrinsic and roll back the removal.

## Proof of Concept
1. `source` calls `create_swap(target, hashed_proof, BalanceSwapAction::new(value), duration)`; `value` is reserved on `source`.
2. `target` is an account with zero/below-existential-deposit balance such that `repatriate_reserved` transferring `value` as free balance would fail.
3. `target` calls `claim_swap(proof, action)`. `swap.action.claim(...)` fails, returning `false`; `PendingSwaps` entry is still removed; `Event::SwapClaimed{success:false}` is emitted; call returns `Ok(())`.
4. `source` later calls `cancel_swap(target, hashed_proof)`; it fails with `Error::NotExist` since the entry no longer exists.
5. `source`'s reserved balance remains permanently stuck — unreachable via both `claim_swap` and `cancel_swap`.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L94-97)
```rust
	fn reserve(&self, source: &AccountId) -> DispatchResult;
	/// Claim the reserved resources, with `source` and `target`. Returns whether the claim
	/// succeeds.
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool;
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-155)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L161-163)
```rust
	fn cancel(&self, source: &AccountId) {
		C::unreserve(source, self.value);
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L262-280)
```rust
			let source = ensure_signed(origin)?;
			ensure!(
				!PendingSwaps::<T>::contains_key(&target, hashed_proof),
				Error::<T>::AlreadyExist
			);

			action.reserve(&source)?;

			let swap = PendingSwap {
				source,
				action,
				end_block: frame_system::Pallet::<T>::block_number() + duration,
			};
			PendingSwaps::<T>::insert(target.clone(), hashed_proof, swap.clone());

			Self::deposit_event(Event::NewSwap { account: target, proof: hashed_proof, swap });

			Ok(())
		}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L311-319)
```rust
			let succeeded = swap.action.claim(&swap.source, &target);

			PendingSwaps::<T>::remove(target.clone(), hashed_proof);

			Self::deposit_event(Event::SwapClaimed {
				account: target,
				proof: hashed_proof,
				success: succeeded,
			});
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L339-347)
```rust
			let swap = PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?;
			ensure!(swap.source == source, Error::<T>::SourceMismatch);
			ensure!(
				frame_system::Pallet::<T>::block_number() >= swap.end_block,
				Error::<T>::DurationNotPassed,
			);

			swap.action.cancel(&swap.source);
			PendingSwaps::<T>::remove(&target, hashed_proof);
```
