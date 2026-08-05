Audit Report

## Title
Silent transfer failures in `claim_child_bounty` permanently lock child-bounty funds and falsely emit `Claimed` - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`claim_child_bounty` performs two currency transfers (curator fee and beneficiary payout) out of the deterministic `child_bounty_account`, but only checks their success with `debug_assert!`, which is a no-op in release builds. If either transfer fails, execution proceeds unconditionally to emit `Claimed`, decrement `ParentChildBounties`, remove the description, and delete the `ChildBounties` storage entry, permanently orphaning the funds remaining in `child_bounty_account`.

## Finding Description
In `claim_child_bounty`, after the unlock-time check, the curator deposit is unreserved and two transfers are issued:
```rust
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());
...
let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());
``` [1](#0-0) 

Neither `Result` is propagated via `?` or checked with `ensure!`; `debug_assert!` is stripped out entirely in non-debug builds, so a transfer failure produces no error and no rollback. Regardless of outcome, the function unconditionally deposits `Event::Claimed`, decrements `ParentChildBounties`, removes `ChildBountyDescriptionsV1`, and sets `*maybe_child_bounty = None`, deleting all on-chain tracking of the child bounty and its account: [2](#0-1) 

The call is reachable by any signed account with no origin restriction to curator/beneficiary: [3](#0-2) 

A concrete failure mode is `Error::ExistentialDeposit`: `T::Currency::transfer(..., AllowDeath)` to a beneficiary/curator account that does not yet exist fails if the transferred amount is below the runtime's Existential Deposit — plausible for a small child-bounty value or a fresh account. Once the `ChildBounties` entry is deleted, there is no dispatchable in the pallet that can re-derive and retry payout from `child_bounty_account`, so the residual balance becomes permanently unreachable.

## Impact Explanation
This matches the "permanent user-fund lock" category in the Impact Gate: funds left in `child_bounty_account` after a failed transfer become permanently stranded once the storage entry is deleted, with no recovery path in the pallet. The `Claimed` event is also falsely emitted despite the transfer failure, misreporting settlement to external observers/indexers.

## Likelihood Explanation
`claim_child_bounty` is public and unprivileged — `ensure_signed(origin)?` with no check that the caller is the curator or beneficiary — callable by anyone once `unlock_at` has passed. No governance, admin, or validator collusion is needed; the triggering condition (payout/curator_fee below ED to a non-existent account) can occur naturally with small child bounties or be deliberately engineered by an attacker choosing bounty parameters and target accounts.

## Recommendation
Replace both `debug_assert!` checks with proper error propagation (e.g., `.map_err(...)?`), and only perform the `Claimed` event emission, `ParentChildBounties` decrement, description removal, and `ChildBounties` deletion after confirming both transfers succeeded. Consider preserving the child-bounty record on transfer failure so a retry remains possible, and review whether `Preservation::Expendable`/keep-alive semantics need to be explicit given ED-related failure modes.

## Proof of Concept
1. Propose and accept a parent bounty; add a child bounty with `fee`/value such that `payout = balance - curator_fee` is below the runtime's Existential Deposit.
2. Assign a curator, have them accept and award the child bounty to a `beneficiary` account that has never existed on-chain.
3. Wait until `unlock_at` is reached, then call `claim_child_bounty(parent_bounty_id, child_bounty_id)` from any signed account.
4. In a release build, `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` fails with `ExistentialDeposit`; `debug_assert!` is compiled out.
5. Despite the failure, `Event::Claimed` fires and the `ChildBounties` entry at `(parent_bounty_id, child_bounty_id)` is deleted, leaving the residual balance in `child_bounty_account` permanently unreachable through any pallet dispatchable.

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
