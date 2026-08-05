Audit Report

## Title
Permanent fund lock in `claim_child_bounty`: payout transfer failures are ignored via `debug_assert!` and bounty state is unconditionally destroyed - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`claim_child_bounty` transfers the curator fee and beneficiary payout from `child_bounty_account_id` using `T::Currency::transfer(..., AllowDeath)`, but only checks the results with `debug_assert!`, which compiles to nothing in release builds. [1](#0-0)  Regardless of whether the payout transfer actually succeeds, the code unconditionally decrements `ParentChildBounties`, removes `ChildBountyDescriptionsV1`, and sets `*maybe_child_bounty = None`, permanently deleting the only storage reference to the escrow account. [2](#0-1) 

## Finding Description
In `claim_child_bounty`, `ensure_signed(origin)?` is the only origin check — any account can call it once `unlock_at` has passed. [3](#0-2)  The fee and payout transfers to `curator` and `beneficiary` use `AllowDeath`, and their `Result`s are discarded except for `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())`, both of which are no-ops outside debug-assertions builds. [1](#0-0)  Immediately after, independent of transfer outcome, the pallet emits a `Claimed` event and unconditionally clears `ParentChildBounties`, `ChildBountyDescriptionsV1`, and the `ChildBounties` entry itself via `*maybe_child_bounty = None`. [4](#0-3)  Because `award_child_bounty` allows the curator to set an arbitrary `beneficiary: AccountIdLookupOf<T>` with no existence or ED check, a payout below the existential deposit sent to a non-existent account causes `Currency::transfer(..., AllowDeath)` to return `TokenError::CannotCreate`, yet cleanup proceeds anyway. Once `*maybe_child_bounty = None` executes, there is no dispatchable call that can reference `(parent_bounty_id, child_bounty_id)` again — `award_child_bounty`, `close_child_bounty`, and `claim_child_bounty` all require `maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?` to succeed, so any balance still held by `child_bounty_account_id` becomes permanently unreachable. Confirming the analog cited in the claim, `pallet-bounties` has a `reclaim_bounty_funds` extrinsic to sweep stranded balances back after a failed payout, but no such mechanism exists in `pallet-child-bounties`. [5](#0-4) 

## Impact Explanation
This results in a permanent lock of on-chain bounty funds in the `child_bounty_account_id` derived account, with no governance, admin, or permissionless path to recover them once the storage record is deleted — this falls under the "permanent user-fund or bridge-state lock" category of the impact gate. The root cause is unconditional state cleanup regardless of settlement success, which requires no privileged actor: any beneficiary account that lacks the existential deposit (either by never having been funded, or by being reaped) combined with any unprivileged signed account invoking `claim_child_bounty` is sufficient to trigger the loss.

## Likelihood Explanation
The precondition is straightforward and requires no cooperation from a privileged party beyond the curator's ordinary use of `award_child_bounty`, which places no constraint on `beneficiary` existence or the resulting `payout` size relative to the existential deposit. A `payout` below ED sent to a never-funded account, or an account reaped after being funded but before claim, deterministically triggers `TokenError::CannotCreate` on `AllowDeath` transfer, and any signed account can then permissionlessly trigger `claim_child_bounty` to seal the funds' fate.

## Recommendation
Propagate `fee_transfer_result` and `payout_transfer_result` with `?` (or explicit `DispatchResult` handling) instead of `debug_assert!`, and only perform the `ParentChildBounties`, `ChildBountyDescriptionsV1`, and `ChildBounties` cleanup after both transfers succeed. Add a permissionless `reclaim_child_bounty_funds` extrinsic analogous to `pallet_bounties::reclaim_bounty_funds` to sweep any balance stranded in orphaned `child_bounty_account_id` accounts. Additionally, consider validating that `beneficiary` can receive the intended `payout` (e.g., existential-deposit check) at `award_child_bounty` time.

## Proof of Concept
1. Curator creates and activates a child bounty with `value` and `fee` such that `payout = value - fee` is below `ExistentialDeposit`.
2. Curator calls `award_child_bounty` with `beneficiary` set to a fresh, never-funded account.
3. After `unlock_at`, any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)`.
4. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err(TokenError::CannotCreate)`; `debug_assert!` is compiled out in the release runtime, so execution proceeds unaffected.
5. `ChildBounties` entry for `(parent_bounty_id, child_bounty_id)` is removed, `ParentChildBounties` decremented, `Claimed` event emitted despite the failed transfer.
6. `payout` remains stuck in `child_bounty_account_id`; no extrinsic can reference the removed index to recover it, confirmed by the absence of any `reclaim_child_bounty_funds`-equivalent call in `substrate/frame/child-bounties/src/lib.rs`.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L686-712)
```rust
		pub fn claim_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?;

			// Ensure child-bounty is in expected state.
			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					if let ChildBountyStatus::PendingPayout {
						ref curator,
						ref beneficiary,
						ref unlock_at,
					} = child_bounty.status
					{
						// Ensure block number is elapsed for processing the
						// claim.
						ensure!(
							Self::treasury_block_number() >= *unlock_at,
							BountiesError::<T>::Premature,
						);
```

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```

**File:** substrate/frame/child-bounties/src/lib.rs (L746-763)
```rust
						// Trigger the Claimed event.
						Self::deposit_event(Event::<T>::Claimed {
							index: parent_bounty_id,
							child_index: child_bounty_id,
							payout,
							beneficiary: beneficiary.clone(),
						});

						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;
```

**File:** substrate/frame/bounties/src/lib.rs (L1-1)
```rust
// This file is part of Substrate.
```
