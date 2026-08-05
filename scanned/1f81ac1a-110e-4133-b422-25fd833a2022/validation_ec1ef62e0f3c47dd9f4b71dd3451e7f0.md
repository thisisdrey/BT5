### Title
Silent failure of beneficiary payout in `claim_child_bounty` permanently locks bounty funds - ([File: substrate/frame/child-bounties/src/lib.rs])

### Summary
The External report's core broken invariant is: a value-push payment to an attacker-influenced beneficiary can fail, and the calling contract does not verify/handle that failure, silently treating the operation as complete while funds become stranded or the flow is blocked. The local analog is `pallet-child-bounties::claim_child_bounty`, where both the curator-fee and beneficiary payout `Currency::transfer` calls are wrapped in `debug_assert!` instead of a checked/propagated result, and the bounty record is deleted from storage regardless of transfer outcome.

### Finding Description
In `claim_child_bounty` [1](#0-0) , the fee and payout transfers use:
```
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());
...
let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());
```
Immediately after, regardless of whether either transfer actually succeeded, the code proceeds to decrement `ParentChildBounties`, remove `ChildBountyDescriptionsV1`, and set `*maybe_child_bounty = None` — i.e., it unconditionally finalizes and deletes the child-bounty state [2](#0-1) .

`debug_assert!` is compiled out entirely in release builds (the mode substrate runtimes are built and run in for production chains), so any transfer failure is *not* detected, not retried, and not surfaced as an error to the caller. The `AllowDeath` preservation mode means the *source* account is allowed to be reaped, but a beneficiary-side failure is still possible in normal conditions — e.g. `payout` (or `curator_fee`) falling below `ExistentialDeposit` for a beneficiary account that does not yet exist, or other token-level constraints on `T::Currency` (holds/freezes) preventing receipt. If that happens, the transfer returns `Err`, the funds remain stuck in `child_bounty_account`, and the very next lines proceed to delete the `ChildBounties` storage entry and its description, removing any code path that ever references `child_bounty_account` again for this bounty.

This mirrors the ParticleExchange bug-class: a push-based payment to a value that can legitimately fail is not verified before the state is finalized as "settled," so settlement diverges from actual fund movement.

### Impact Explanation
Once the storage entry is removed, there is no remaining mechanism in the pallet to retry the payout or recover the balance left in `child_bounty_account` — the funds are permanently locked/orphaned relative to the beneficiary and curator, satisfying "permanent user-fund lock" and "duplicate/incorrect settlement" (state says paid, but funds were not moved) under the impact gate. `claim_child_bounty` is a fully public, unprivileged extrinsic — `ensure_signed(origin)?` with no additional permission check — so any account can trigger the finalization path once the bounty reaches `PendingPayout` and its unlock period has elapsed.

### Likelihood Explanation
Triggering requires only a `payout` or `curator_fee` value that fails on transfer to its beneficiary/curator (e.g., a dust amount below `ExistentialDeposit` for a non-existent account). The curator (who is not necessarily a privileged/root actor — just whoever proposed/was assigned as curator of the child bounty) chooses/awards the beneficiary and controls `child_bounty.fee`, and the amounts can be arranged to reproduce this dust condition. No admin, governance, validator, or off-chain actor is needed; the vulnerable code path executes deterministically for anyone calling `claim_child_bounty` under normal on-chain conditions.

### Recommendation
Do not use `debug_assert!` to gate on-chain fund-moving results. Propagate transfer errors with `?` (or explicitly branch on `Err`) before removing the child-bounty state; if a transfer fails, keep the bounty in a retryable "payout failed" status (similar to `PaymentState::Failed` used elsewhere, e.g. `multi-asset-bounties`) instead of unconditionally deleting the record, so funds can always be recovered/retried by the correct beneficiary.

### Proof of Concept
1. Curator creates and gets assigned to a child bounty with `fee` set such that `payout = value - fee` is a very small amount (below `T::Currency::minimum_balance()` for the destination `AccountId`).
2. Curator calls `award_child_bounty(parent_id, child_id, beneficiary)` with `beneficiary` an account that has never held a balance (does not exist on-chain).
3. After `unlock_at` elapses, any signed account calls `claim_child_bounty(parent_id, child_id)`.
4. Inside `claim_child_bounty`, `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err` because `payout < ExistentialDeposit` and the destination account cannot be created.
5. Because the check is only `debug_assert!` (a no-op in release/production runtime builds), the error is silently discarded.
6. Execution continues: `ChildBounties` entry is set to `None`, `ChildBountyDescriptionsV1` removed, `ParentChildBounties` count decremented, and a `Claimed` event is emitted claiming the payout succeeded.
7. The `payout` balance remains stranded in `child_bounty_account`, unreachable by any remaining dispatchable for this now-deleted child bounty — a permanent fund lock, while on-chain state/events falsely indicate settlement occurred. [3](#0-2)

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L726-765)
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

						Ok(())
```
