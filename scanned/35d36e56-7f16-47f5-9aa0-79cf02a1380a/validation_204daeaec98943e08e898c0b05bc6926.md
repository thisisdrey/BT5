## Title
`claim_swap` unconditionally deletes the pending swap and returns `Ok(())` even when settlement fails, permanently locking the source's reserved funds - (File: `substrate/frame/atomic-swap/src/lib.rs`)

## Summary
`claim_swap` treats the extrinsic itself as "successful" (returns `DispatchResult::Ok(())`) and removes the `PendingSwaps` storage entry *regardless* of whether the underlying value transfer (`swap.action.claim(...)`) actually succeeded. The boolean result of `claim()` is only surfaced in the `SwapClaimed { success }` event, not used to gate storage cleanup or the dispatch outcome. [1](#0-0) 

## Finding Description
In `claim_swap`, the sequence is:
1. `swap.action.claim(&swap.source, &target)` is invoked; for the built-in `BalanceSwapAction` this calls `C::repatriate_reserved(source, target, value, BalanceStatus::Free).is_ok()` and stores the boolean in `succeeded`.
2. `PendingSwaps::<T>::remove(target.clone(), hashed_proof)` runs unconditionally, whether `succeeded` is `true` or `false`.
3. An event `SwapClaimed { success: succeeded }` is deposited, but the extrinsic still returns `Ok(())`. [2](#0-1) [3](#0-2) 

Because the `PendingSwap` record is deleted even on a failed `claim()`, the only other public follow-up path, `cancel_swap`, becomes permanently unusable for that swap: it looks up `PendingSwaps::<T>::get(&target, hashed_proof)` and returns `Error::NotExist` once the record is gone. [4](#0-3) 

If `repatriate_reserved` fails for any reason (e.g. the target account cannot receive the funds because it would remain below the Existential Deposit and can't be created, or the source's reserved balance available for repatriation is less than `value` due to other concurrent reservations/slashes on the same account), then:
- No funds are moved to the target (the "claim" did not settle).
- The source's originally reserved balance is never unreserved (unlike `cancel_swap`, which explicitly calls `swap.action.cancel(&swap.source)` to unreserve).
- The `PendingSwaps` entry that both `claim_swap` and `cancel_swap` depend on is deleted.

The result is a "semantically incomplete success state": the dispatch call succeeds, an event is emitted, but no consumer (source or target) can subsequently interact with the object — `cancel_swap` sees `NotExist` and can never unreserve, and there is no other entry point to release the reservation. Since `target` is an unprivileged, attacker-controlled signer of `claim_swap`, and any account can be configured/left in a state (e.g., zero free balance, no existing `AccountData`) that causes `repatriate_reserved` to fail, an attacker acting as the swap's `target` can trigger this failure path unilaterally and for free, permanently locking the `source`'s reserved balance.

## Impact Explanation
This falls under "permanent user-fund or bridge-state lock," one of the accepted impact categories: an unprivileged actor (the `target` of a swap, who need not cooperate honestly) can cause the source's reserved balance to become permanently stuck with no on-chain recovery path, since the bookkeeping record required by the only cleanup extrinsic (`cancel_swap`) is destroyed regardless of whether settlement occurred.

## Likelihood Explanation
The precondition (target account causing `repatriate_reserved` to fail, e.g. by never having/maintaining the Existential Deposit, or by having other pallets reduce its reserved balance) is entirely within the attacker's control since the attacker is the `target` calling `claim_swap` and chooses when/whether to fund their own account. No privileged access, collusion, or infrastructure compromise is required — a target can simply let their account sit below the ED and then claim, or otherwise engineer a `repatriate_reserved` failure, to grief the source. This is a straightforward, repeatable griefing vector with no cost beyond a normal transaction fee.

## Recommendation
`claim_swap` should only clear the `PendingSwaps` entry when `swap.action.claim(...)` returns `true` (i.e., actual settlement succeeded). On failure, either:
- Leave the entry in place so the source can retry claim conditions being fixed, and/or still allow `cancel_swap` to succeed after `end_block` (source can recover funds), or
- Explicitly call `swap.action.cancel(&swap.source)` to unreserve the source's funds before removing the entry, mirroring the `cancel_swap` cleanup path, and only then remove the record.

Additionally, consider surfacing the failure via `DispatchResult` (or a distinguishable dispatch outcome) rather than silently succeeding with only an event flag, so downstream tooling/consumers don't misinterpret dispatch success as settlement success.

## Proof of Concept
1. `source` creates a swap via `create_swap(target, hashed_proof, BalanceSwapAction::new(value), duration)`, reserving `value` from `source`.
2. `target` is an account that is not yet created (or has been reaped to zero, below `ExistentialDeposit`), and does not top up before claiming.
3. `target` reveals the proof and calls `claim_swap(proof, action)`.
4. `swap.action.claim(source, target)` internally calls `repatriate_reserved(source, target, value, BalanceStatus::Free)`, which fails because crediting `target`'s account with `value` (Free) does not meet ED requirements for account creation (or any other condition causing the repatriation to error) → `succeeded = false`.
5. Despite `succeeded == false`, `PendingSwaps::<T>::remove(target, hashed_proof)` still executes, and `claim_swap` returns `Ok(())`.
6. `source`, after `end_block` passes, calls `cancel_swap(target, hashed_proof)` expecting to reclaim their reserved funds — this now fails with `Error::NotExist` because the record was already deleted in step 5.
7. `source`'s `value` remains reserved indefinitely with no pallet-provided path to release it. [5](#0-4) [6](#0-5)

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-164)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}

	fn weight(&self) -> Weight {
		T::DbWeight::get().reads_writes(1, 1)
	}

	fn cancel(&self, source: &AccountId) {
		C::unreserve(source, self.value);
	}
}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L297-322)
```rust
		pub fn claim_swap(
			origin: OriginFor<T>,
			proof: Vec<u8>,
			action: T::SwapAction,
		) -> DispatchResult {
			ensure!(proof.len() <= T::ProofLimit::get() as usize, Error::<T>::ProofTooLarge);

			let target = ensure_signed(origin)?;
			let hashed_proof = blake2_256(&proof);

			let swap =
				PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::InvalidProof)?;
			ensure!(swap.action == action, Error::<T>::ClaimActionMismatch);

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
