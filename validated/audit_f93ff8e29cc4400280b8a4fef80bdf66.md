I found a solid local analog. The core broken invariant from the Putty report — a counterparty escrows/reserves value, but the step that returns/settles that value can legitimately fail (due to an incompatible asset/account), and the protocol proceeds as if it succeeded, permanently locking the escrowed funds — maps directly onto `pallet-atomic-swap`'s `claim_swap` logic.

### Title
Atomic-swap `claim_swap` permanently locks reserved funds when `SwapAction::claim` fails, by deleting the swap record unconditionally - (File: `substrate/frame/atomic-swap/src/lib.rs`)

### Summary
`pallet_atomic_swap::claim_swap` calls `swap.action.claim(&swap.source, &target)`, which returns a `bool` indicating whether the underlying settlement succeeded, but the pallet removes the `PendingSwaps` entry and returns `Ok(())` regardless of that boolean's value. If `claim` fails (e.g. the target-side transfer legitimately cannot complete), the funds `reserve()`d by the source at `create_swap` time remain reserved forever, because the only path back to them (`cancel_swap`) requires the now-deleted `PendingSwaps` entry to still exist.

### Finding Description
`create_swap` reserves the source's resources via `action.reserve(&source)?` [1](#0-0) . This is the "escrow" step, directly analogous to the buyer paying the premium/depositing baseAsset in the Putty report.

`claim_swap` then does the settlement: [2](#0-1) 
Note that `swap.action.claim(&swap.source, &target)` returns `bool`, and irrespective of `succeeded`, `PendingSwaps::<T>::remove(...)` executes and the extrinsic returns `Ok(())`. The `SwapAction` trait explicitly documents that `claim` "Returns whether the claim succeeds" — i.e., failure is an expected, first-class outcome, not a bug condition [3](#0-2) .

For the built-in `BalanceSwapAction`, `claim` is implemented as: [4](#0-3) 
`repatriate_reserved` can fail for ordinary, non-malicious reasons — e.g. the target account has never been created and the transferred amount would leave it below the existential deposit, or the target account was reaped between `create_swap` and `claim_swap`. This is exactly the "counterparty side doesn't support the expected receive operation" scenario from the Putty bug (`safeTransferFrom` reverting on non-ERC721-compliant NFTs like CryptoPunks).

Once `claim` returns `false`:
- The `PendingSwaps` entry is deleted (line 313), so `cancel_swap`'s lookup `PendingSwaps::<T>::get(&target, hashed_proof).ok_or(Error::<T>::NotExist)?` will always fail from that point on [5](#0-4) .
- `BalanceSwapAction::cancel` (the only other code path that calls `C::unreserve`) can therefore never be invoked for this swap [6](#0-5) .
- The reserved balance on the `source` account stays reserved indefinitely — it is neither transferred to the target nor returned to the source. This is a permanent user-fund lock.

### Impact Explanation
This is a permanent user-fund lock triggered without any privileged actor: any unsigned target account that legitimately can't receive the repatriated balance (e.g. a fresh account below ED, or any custom `SwapAction` implementation whose `claim` can genuinely fail for asset-specific reasons — the same class of failure as the CryptoPunks/`onERC721Received` issue) causes the source's escrowed funds to become unrecoverable. This satisfies the "permanent user-fund ... lock" impact criterion.

### Likelihood Explanation
Likelihood is moderate-to-high for real-world deployments of this pallet: no adversarial coordination or privileged role is required — the target simply needs to be an account (or asset path) for which the settlement transfer fails, which can happen accidentally (unfunded/reaped account) or be engineered by a target who wants to keep the source's funds locked forever out of spite, with zero cost to themselves (they never need to fund their account or otherwise cooperate).

### Recommendation
`claim_swap` must not unconditionally delete `PendingSwaps` when `claim` reports failure. Either:
- Only remove the pending swap and emit `SwapClaimed{success:true}` on `claim() == true`; on failure, leave the entry so `cancel_swap` remains available after `end_block`, or
- Change `SwapAction::claim` to return a `DispatchResult` and propagate the error, aborting the whole extrinsic (rolling back the removal) so the source can retry `claim_swap` or later call `cancel_swap`.

### Proof of Concept
1. `source` calls `create_swap(target, hashed_proof, BalanceSwapAction::new(value), duration)` — `value` is reserved on `source`.
2. Ensure `target` is an account with zero existing balance (below existential deposit) such that transferring `value` as free balance via `repatriate_reserved` would fail (or is reaped in between).
3. `target` reveals the proof and calls `claim_swap(proof, action)`. `swap.action.claim(...)` calls `repatriate_reserved`, which errors, so `claim` returns `false`. The pallet still deletes `PendingSwaps` and returns `Ok(())`, emitting `SwapClaimed{success:false}`.
4. `source` later calls `cancel_swap(target, hashed_proof)` — fails with `Error::NotExist` because the entry was already removed.
5. `source`'s reserved balance is now permanently stuck: unreachable by `claim_swap` (record gone) and unreachable by `cancel_swap` (record gone).

### Citations

**File:** substrate/frame/atomic-swap/src/lib.rs (L91-102)
```rust
pub trait SwapAction<AccountId, T: Config> {
	/// Reserve the resources needed for the swap, from the given `source`. The reservation is
	/// allowed to fail. If that is the case, the the full swap creation operation is cancelled.
	fn reserve(&self, source: &AccountId) -> DispatchResult;
	/// Claim the reserved resources, with `source` and `target`. Returns whether the claim
	/// succeeds.
	fn claim(&self, source: &AccountId, target: &AccountId) -> bool;
	/// Weight for executing the operation.
	fn weight(&self) -> Weight;
	/// Cancel the resources reserved in `source`.
	fn cancel(&self, source: &AccountId);
}
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

**File:** substrate/frame/atomic-swap/src/lib.rs (L332-344)
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
```
