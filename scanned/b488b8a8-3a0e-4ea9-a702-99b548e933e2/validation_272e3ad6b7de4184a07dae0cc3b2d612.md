## Title
Atomic Swap pallet permanently locks reserved funds on failed claim due to unconditional storage removal - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
The Entropy report's core broken invariant is that a two-party commit/reveal protocol can be steered so that one party's failure/refusal to complete their half of the protocol deterministically and silently extracts or locks value belonging to the other party, with the failure looking like ordinary bad luck rather than an exploit. `pallet-atomic-swap` implements the analogous two-party commit-reveal pattern (`create_swap` → reserve, `claim_swap` → reveal proof and settle, `cancel_swap` → refund after timeout) fully on-chain, and contains a concrete local flaw: `claim_swap` deletes the pending swap from storage unconditionally, even when the underlying settlement action reports failure, leaving the source's reserved funds permanently unrecoverable.

### Finding Description
`claim_swap` computes whether the settlement actually succeeded but ignores that result when deciding whether to remove the swap record: [1](#0-0) 

`succeeded` comes from `BalanceSwapAction::claim`, which wraps `Currency::repatriate_reserved` and can return `false` if the source's reserved balance is insufficient at the moment of the claim: [2](#0-1) 

`ReservableCurrency::reserve`/`unreserve` operate on a single, un-tagged reserved-balance counter per account, not a swap-specific escrow. Anything else that consumes or reduces `source`'s generic reserved balance between `create_swap` and `claim_swap` (concurrent reserves/unreserves from any other pallet using the same `Currency`, partial slashes, etc.) can leave less than `value` reserved at claim time, causing `repatriate_reserved` to fail and `succeeded` to be `false`.

Regardless of that outcome, `PendingSwaps::<T>::remove(target.clone(), hashed_proof)` still executes unconditionally, and only the event flags the failure — the record is gone forever: [3](#0-2) 

Because the entry keyed by `(target, hashed_proof)` no longer exists, `cancel_swap` — the only other path that can release `source`'s reservation via `action.cancel` — now fails with `Error::NotExist`, since it looks up that exact same key: [4](#0-3) 

The reservation created in `create_swap` via `action.reserve(&source)` therefore has no remaining dispatchable path to be released: [5](#0-4) 

This is the direct local analog of the report's "combined-value"/"non-reveal" collusion scenario: the protocol's correctness depends on an implicit non-collusion/no-interference assumption between the two independent parties (and any other pallet sharing the same account's reserve pool), which is neither documented nor enforced, and whose violation converts a normal failed-claim event into permanent fund loss rather than a retryable or refundable state.

### Impact Explanation
This is a permanent user-fund lock triggered purely through the pallet's own public extrinsics (`create_swap`, `claim_swap`, `cancel_swap`), with no validator, governance, relayer, or admin involvement required — matching the "permanent user-fund ... lock" and "duplicate settlement or payout state must only advance after ... settlement succeed[s] atomically" criteria in the impact gate. The corrupted value is the `PendingSwaps<T>` storage entry for `(target, hashed_proof)`: it is destroyed on the first claim attempt independent of whether `SwapAction::claim` actually moved funds, permanently stranding `source`'s reserved balance with no remaining code path (`cancel_swap` requires the now-deleted entry) to unreserve it.

### Likelihood Explanation
Triggering requires only that `source`'s generic reserved balance be reduced below `value` by the time `claim_swap` executes — plausible whenever the same account participates in any other reserve-consuming activity on a production runtime (this pallet uses the legacy, untagged `ReservableCurrency` reserve pool shared across all pallets), or is deliberately engineered by `source` itself to grief a counterparty in a cross-chain swap (target already burned their leg on the other chain believing the proof reveal would settle this one). The bug does not depend on chance timing of block production or any validator behavior, only on ordinary account state at claim time, so it is fully reachable by unprivileged signed accounts.

### Recommendation
In `claim_swap`, only remove the `PendingSwaps` entry when `succeeded` is `true`; on failure, either leave the entry intact (so `source` can later `cancel_swap` after `end_block`, or `target` can retry the claim once sufficient reserved balance exists) or perform the removal but explicitly `unreserve`/refund the source's remaining stake immediately as part of the same failed-claim path so no dispatchable value can become permanently stranded.

### Proof of Concept
1. `A` (source) calls `create_swap(target = B, hashed_proof = H, action = BalanceSwapAction::new(V), duration = D)`. This reserves `V` from `A`'s account via `action.reserve(&A)` (`lib.rs:268`).
2. Before `B` claims, `A`'s reserved balance (the single, untagged reserve counter) is reduced below `V` by any independent legitimate operation on the same account that calls `unreserve`/`slash_reserved` against the shared reserve pool (e.g., another pallet configured with the same `Currency`, or a scenario `A` sets up itself).
3. `B` calls `claim_swap(proof, action)` with the correct pre-image of `H`. `swap.action.claim(&A, &B)` → `Currency::repatriate_reserved` fails because `A`'s reserved balance is now `< V`, so `succeeded = false` (`lib.rs:153-155`, `lib.rs:311`).
4. Despite `succeeded == false`, `PendingSwaps::<T>::remove(B, H)` still executes (`lib.rs:313`), and `Event::SwapClaimed { success: false }` is emitted — the only observable signal, easily mistaken for routine failure.
5. `A` later calls `cancel_swap(target = B, hashed_proof = H)` after `end_block`. Lookup `PendingSwaps::<T>::get(&B, H)` returns `None`, so the call returns `Error::NotExist` (`lib.rs:339`), and `action.cancel(&A)` (which would call `C::unreserve`) never executes.
6. `A`'s remaining reserved balance (whatever amount is left under `V`) stays reserved indefinitely with no remaining pallet call able to release it.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L153-155)
```rust
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool {
		C::repatriate_reserved(source, target, self.value, BalanceStatus::Free).is_ok()
	}
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L263-268)
```rust
			ensure!(
				!PendingSwaps::<T>::contains_key(&target, hashed_proof),
				Error::<T>::AlreadyExist
			);

			action.reserve(&source)?;
```

**File:** substrate/frame/atomic-swap/src/lib.rs (L307-319)
```rust
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
