## Analysis

The core broken invariant in the external report is: **a time-bound commitment that one party relies on to become "safely reclaimable/void" after a deadline can still be unexpectedly fulfilled/settled by the counterparty**, causing loss of already-reserved value to the wrong party. Looking for a local, non-privileged, non-front-run analog of this exact class of bug (missing symmetric deadline enforcement between the "claim" and "cancel" paths of a two-party pending commitment) leads to `pallet-atomic-swap`. [1](#0-0) 

### Title
Missing deadline check in `AtomicSwap::claim_swap` lets the target claim reserved funds indefinitely, defeating the source's time-bound reclaim guarantee - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
`pallet-atomic-swap`'s `create_swap` reserves the source's funds and records an `end_block` (`swap.end_block`) as the deadline for the swap. `cancel_swap` correctly enforces that the source can only reclaim reserved funds once `block_number >= swap.end_block`. However, `claim_swap` — the target's counterpart action — performs no such check at all, so the target can claim (and drain) the reserved funds at any time, including long after `end_block` has passed, as long as the `PendingSwaps` entry has not yet been removed by a `cancel_swap` call.

### Finding Description
`do_create_swap`/`create_swap` reserves `value` from `source` and stores a `PendingSwap { source, action, end_block }` keyed by `(target, hashed_proof)`: [2](#0-1) 

`cancel_swap` enforces the deadline before returning reserved funds to `source`: [3](#0-2) 

But `claim_swap`, called by `target` with the revealed `proof`, never checks `frame_system::Pallet::<T>::block_number()` against `swap.end_block` before calling `swap.action.claim(...)` and repatriating the reserved balance from `source` to `target`: [1](#0-0) 

This is inconsistent with the sibling implementation in `pallet-nfts`'s atomic-swap feature, which does enforce a symmetric deadline check on the claim side (`ensure!(now <= swap.deadline, Error::<T, I>::DeadlineExpired)`), proving that a deadline check on claim is the intended, standard safety property for this exact commit/claim/cancel pattern: [4](#0-3) 

The corrupted value is `PendingSwap.end_block` (`substrate/frame/atomic-swap/src/lib.rs:79`): it is treated as an authoritative deadline on the `cancel` path but is silently ignored on the `claim` path.

### Impact Explanation
`create_swap` reserves the source's balance for the full swap `value` via `ReservableCurrency::reserve`. The source's mental/operational model (and the pallet's own doc comment, which explicitly discusses timing safety around `end_block`) is that once `end_block` passes without a claim, the source is free to treat the reservation as expired and can recover the funds via `cancel_swap`. Because `claim_swap` has no deadline gate, the target can hold the revealed proof and submit `claim_swap` at any later block — even long after the source (or an off-chain/cross-chain counterpart process) has concluded the swap failed and moved on — as long as `cancel_swap` was not already executed. This can result in unexpected, unbacked repatriation of reserved value to the target with no way for the source to prevent it once the proof is known, other than racing to submit `cancel_swap` first. This falls under "theft or unbacked mint/unlock" / "duplicate settlement" impact for balances held via `ReservableCurrency`.

### Likelihood Explanation
This requires no privileged actor, no validator/collator/relayer compromise, and no governance action — it is exploitable by any two unprivileged accounts using the pallet's own public extrinsics (`create_swap`, `claim_swap`, `cancel_swap`) exactly as designed, provided the target simply withholds the proof past `end_block` and submits `claim_swap` whenever convenient before the source calls `cancel_swap`. No front-running of another party's imminent transaction is needed — the target can wait arbitrarily long unilaterally.

### Recommendation
Add an explicit deadline check to `claim_swap`, mirroring `cancel_swap` and the `pallet-nfts` implementation, e.g. `ensure!(frame_system::Pallet::<T>::block_number() < swap.end_block, Error::<T>::DurationPassed);` before calling `swap.action.claim(...)`.

### Proof of Concept
1. `source` calls `create_swap(target, hashed_proof, action=BalanceSwapAction::new(V), duration=D)` at block `N`. `V` is reserved from `source`; `end_block = N + D`.
2. Block `N + D` passes with no claim. `source`, believing the swap has expired per the documented protocol assumption, does not immediately call `cancel_swap` (e.g., it is not on the critical path, or the transaction is simply not yet included).
3. At block `N + D + k` (arbitrary `k`), `target` submits `claim_swap(proof, action)`. Since `claim_swap` never checks `end_block`, the call succeeds: `swap.action.claim(&source, &target)` repatriates the reserved `V` from `source` to `target`, and the pending swap entry is removed.
4. `source` never regains the `V` it assumed was reclaimable after the deadline; `cancel_swap`, if attempted after this point, fails with `Error::NotExist` since the entry is already removed.

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L255-280)
```rust
		pub fn create_swap(
			origin: OriginFor<T>,
			target: T::AccountId,
			hashed_proof: HashedProof,
			action: T::SwapAction,
			duration: BlockNumberFor<T>,
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

**File:** substrate/frame/atomic-swap/src/lib.rs (L330-352)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::DbWeight::get().reads_writes(1, 1).ref_time().saturating_add(40_000_000))]
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

**File:** substrate/frame/nfts/src/features/atomic_swap.rs (L190-191)
```rust
		let now = T::BlockNumberProvider::current_block_number();
		ensure!(now <= swap.deadline, Error::<T, I>::DeadlineExpired);
```
