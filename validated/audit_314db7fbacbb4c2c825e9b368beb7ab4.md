The claim is verified by direct code inspection of the pallet.

Audit Report

## Title
`claim_swap` unconditionally deletes the pending swap and returns `Ok(())` even when settlement fails, permanently locking the source's reserved funds - (File: `substrate/frame/atomic-swap/src/lib.rs`)

## Summary
`claim_swap` invokes `swap.action.claim(&swap.source, &target)`, which for `BalanceSwapAction` calls `C::repatriate_reserved(source, target, value, BalanceStatus::Free).is_ok()`, and captures the boolean result into `succeeded`. Regardless of whether `succeeded` is `true` or `false`, the pallet unconditionally executes `PendingSwaps::<T>::remove(target.clone(), hashed_proof)` and returns `Ok(())`, with only the `SwapClaimed { success: succeeded }` event reflecting the actual outcome. [1](#0-0) 

## Finding Description
The `SwapAction::claim` implementation for `BalanceSwapAction` returns a boolean based on `repatriate_reserved`'s success, but does not itself unreserve or otherwise clean up on failure: [2](#0-1) 

In `claim_swap`, the storage removal happens immediately after `claim()` is called, with no branch on `succeeded`: [3](#0-2) 

Compare this to `cancel_swap`, which explicitly calls `swap.action.cancel(&swap.source)` (which calls `C::unreserve`) before removing the record — this unreserve step is entirely missing from the failure path of `claim_swap`: [4](#0-3) 

Because `PendingSwaps::<T>::remove` executes unconditionally, if `repatriate_reserved` fails (e.g., crediting `target`'s account with the free balance would leave it below the Existential Deposit and it can't be created, or the source's reserved balance available for repatriation is otherwise insufficient), the record backing the swap disappears. Since `cancel_swap` requires `PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?` to succeed before it can call `swap.action.cancel(&swap.source)` to unreserve the source's funds, the source loses the only on-chain path to reclaim their reserved balance once the record is gone: [5](#0-4) 

Since `claim_swap` is a public extrinsic callable by any signed account acting as `target`, an attacker who is the designated target of a swap can trigger a `repatriate_reserved` failure (e.g., by simply not maintaining ED on their account) and unilaterally cause the source's funds to become permanently locked, with `claim_swap` still returning `Ok(())` masking the failure at the dispatch level.

## Impact Explanation
This matches the "permanent user-fund... lock" impact category in the accepted impact gate. An unprivileged actor (the swap's `target`) can, without cooperation from or consent of the `source`, cause the source's previously reserved balance to become permanently un-recoverable on-chain, because the only cleanup path (`cancel_swap`) depends on a `PendingSwaps` entry that is destroyed regardless of settlement success. The corrupted state is the `PendingSwaps` storage entry being removed prematurely while the source's reserved balance (in `pallet-balances`) is never unreserved.

## Likelihood Explanation
The precondition is entirely within the attacker's control: as the `target` of a swap, they choose when to call `claim_swap`, and can trivially arrange for their own account to be in a state that causes `repatriate_reserved` to fail (e.g., account not yet in existence / below Existential Deposit, so crediting it via `BalanceStatus::Free` fails to satisfy account-creation requirements). No privileged access, collusion, or off-chain infrastructure is required, and the attack costs only a normal transaction fee, making it straightforward and repeatable against any `source` who creates a swap directed at such a `target`.

## Recommendation
Gate the `PendingSwaps::<T>::remove` call (and dispatch semantics) on the `succeeded` result of `swap.action.claim(...)`:
- On success, remove the entry as today.
- On failure, either leave the entry so it can still be cleaned up by `cancel_swap` after `end_block`, or explicitly call `swap.action.cancel(&swap.source)` to unreserve the source's funds before removing the entry, mirroring the `cancel_swap` cleanup path.
Consider also making the dispatch outcome reflect settlement failure rather than relying solely on the `SwapClaimed { success }` event flag.

## Proof of Concept
1. `source` calls `create_swap(target, hashed_proof, BalanceSwapAction::new(value), duration)`, which reserves `value` from `source` via `action.reserve(&source)`. [6](#0-5) 
2. `target` is an account with no existing `AccountData` / below Existential Deposit and does not top up.
3. `target` calls `claim_swap(proof, action)`; internally `swap.action.claim(&swap.source, &target)` calls `repatriate_reserved(source, target, value, BalanceStatus::Free)`, which fails because crediting `target` does not satisfy account-creation/ED requirements, so `succeeded = false`. [2](#0-1) 
4. Despite `succeeded == false`, `PendingSwaps::<T>::remove(target.clone(), hashed_proof)` executes and `claim_swap` returns `Ok(())`. [1](#0-0) 
5. After `end_block`, `source` calls `cancel_swap(target, hashed_proof)`, which fails with `Error::NotExist` because the record was already removed in step 4, leaving `source`'s `value` permanently reserved with no path to release it. [7](#0-6)

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-155)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L261-280)
```rust
		) -> DispatchResult {
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

**File:** substrate/frame/atomic-swap/src/lib.rs (L311-322)
```rust
			let succeeded = swap.action.claim(&swap.source, &target);

			PendingSwaps::<T>::remove(target.clone(), hashed_proof);

			Self::deposit_event(Event::SwapClaimed {
				account: target,
				proof: hashed_proof,
				success: succeeded,
			});

			Ok(())
		}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L332-352)
```rust
		pub fn cancel_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
		) -> DispatchResult {
			let source = ensure_signed(origin)?;

			let swap = PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?;
			ensure!(swap.source == source, Error::<T>::SourceMismatch);
			ensure!(
				frame_system::Pallet::<T>::block_number() >= swap.end_block,
				Error::<T>::DurationNotPassed,
			);

			swap.action.cancel(&swap.source);
			PendingSwaps::<T>::remove(&target, hashed_proof);

			Self::deposit_event(Event::SwapCancelled { account: target, proof: hashed_proof });

			Ok(())
		}
```
