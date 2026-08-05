Audit Report

## Title
Bounty and child-bounty payout transfers rely on `debug_assert!` instead of checked results, allowing state to advance to "Claimed"/removed even when the payout transfer silently fails - (File: substrate/frame/child-bounties/src/lib.rs)

## Summary
In `claim_child_bounty()` [1](#0-0)  and `claim_bounty()` [2](#0-1) , the curator-fee and beneficiary `T::Currency::transfer` results are checked only with `debug_assert!`, which is compiled out in release builds. Both functions unconditionally proceed to emit the claimed event and delete the bounty/child-bounty record regardless of whether the transfer actually succeeded.

## Finding Description
`claim_child_bounty()` computes `curator_fee` and `payout` from the child bounty account's own `free_balance` [3](#0-2) , then performs two transfers whose `DispatchResult`s are discarded except for a `debug_assert!` [4](#0-3) . Immediately after, the function emits `Event::Claimed`, decrements `ParentChildBounties`, removes `ChildBountyDescriptionsV1`, and sets `*maybe_child_bounty = None` [5](#0-4) , none of which is gated on the transfer results. The identical pattern exists in `claim_bounty()` in the parent pallet, where `res.is_ok()` checks via `debug_assert!` precede unconditional deletion of the bounty record and emission of `Event::BountyClaimed` [6](#0-5) . Because `debug_assert!` is a no-op when `cfg(debug_assertions)` is disabled (the default for release/production runtime builds), a failing transfer in production has zero effect on control flow, letting the storage-removal and event-emission proceed as if the payout succeeded.

## Impact Explanation
If the transfer fails post-checks, the computed `payout`/`curator_fee` funds remain stranded in the sovereign `child_bounty_account_id`/`bounty_account_id`, which becomes unreachable once the bounty index is removed from storage — matching the "permanent user-fund lock" and "duplicate/incorrect settlement" impact categories, since the chain state and events report the payout as delivered while no value moved.

## Likelihood Explanation
Likelihood is low. Both transfer amounts (`curator_fee`, `payout`) are derived directly from the sub-account's own `free_balance`, so a standard `pallet-balances` `Currency` implementation using `AllowDeath` essentially never fails on this call path for well-formed state. The scenario requires either a non-standard `T::Currency` adapter with unusual failure conditions, or an edge case in hold/freeze interaction not covered by the "should not fail" assumptions in the comments. No privileged actor is needed — a normal signed caller invoking the already-permissionless `claim_bounty`/`claim_child_bounty` extrinsics would trigger the bug path if such a transfer failure condition existed.

## Recommendation
Replace `debug_assert!(result.is_ok())` with proper error propagation (`?` or `ensure!`) at both call sites in `claim_bounty()` (`substrate/frame/bounties/src/lib.rs`) and `claim_child_bounty()` (`substrate/frame/child-bounties/src/lib.rs`) so that a failed currency transfer aborts the extrinsic via the enclosing `try_mutate_exists` closure rather than allowing the bounty record to be marked claimed/removed.

## Proof of Concept
1. Configure a runtime built in release mode (`cfg(debug_assertions)` disabled) with a `pallet-child-bounties::Config::Currency` whose `transfer` can return `Err` for the specific beneficiary/amount reached by `claim_child_bounty`.
2. Progress a child bounty through `add_child_bounty` → `propose_curator` → `accept_curator` → `award_child_bounty` to `ChildBountyStatus::PendingPayout`.
3. After `unlock_at`, call `claim_child_bounty(origin, parent_bounty_id, child_bounty_id)` as any signed account.
4. With `debug_assertions` disabled, a failing `T::Currency::transfer` call has no effect on control flow: `Event::Claimed` is emitted, `ParentChildBounties` is decremented, `ChildBountyDescriptionsV1` is removed, and `*maybe_child_bounty = None` is set.
5. Result: `payout`/`curator_fee` remain stuck in `child_bounty_account_id` with no remaining bounty record to retry or recover the funds, while chain state/events report successful settlement.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L715-719)
```rust
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);
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

**File:** substrate/frame/bounties/src/lib.rs (L812-837)
```rust
					let err_amount = T::Currency::unreserve(&curator, bounty.curator_deposit);
					debug_assert!(err_amount.is_zero());

					// Get total child bounties curator fees, and subtract it from the parent
					// curator fee (the fee in present referenced bounty, `self`).
					let children_fee = T::ChildBountyManager::children_curator_fees(bounty_id);
					debug_assert!(children_fee <= fee);

					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
					T::ChildBountyManager::bounty_removed(bounty_id);

					Self::deposit_event(Event::<T, I>::BountyClaimed {
						index: bounty_id,
						payout,
						beneficiary,
					});
```
