### Title
Child-bounty payout transfer failures are silently ignored via `debug_assert!`, causing fund loss when `claim_child_bounty` deletes state unconditionally - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
The Cooler bug pattern is: a function deletes accounting state for an amount owed to a party without first guaranteeing that amount was actually settled to that party. The local analog is `claim_child_bounty` in `pallet-child-bounties`, which performs the curator-fee and beneficiary transfers using results that are only checked with `debug_assert!` (a no-op in release/production builds), then unconditionally removes the child-bounty record regardless of whether the transfers actually succeeded.

### Finding Description
In `claim_child_bounty`, the fee and payout transfers are made and their results assigned to `fee_transfer_result` / `payout_transfer_result`, but the only check performed is `debug_assert!(fee_transfer_result.is_ok())` and `debug_assert!(payout_transfer_result.is_ok())`: [1](#0-0) 

Regardless of the outcome of these transfers, the function proceeds to decrement the active-count, remove the description, and delete the `ChildBounty` storage entry entirely: [2](#0-1) 

`debug_assert!` macros are compiled out in non-debug builds (which is how production runtimes are built), so in the actual chain runtime there is no enforcement that the transfers succeeded. If `T::Currency::transfer` for either the curator-fee or the beneficiary payout returns an `Err` (e.g. due to unexpected freezes/locks/holds on the `child_bounty_account`, ED edge cases, or any other reason the currency implementation can reject a transfer), the funds remain stuck in the (now orphaned) `child_bounty_account`, while the `ChildBounties` storage entry that tracked this obligation, the curator deposit reservation state, and the parent's `ParentChildBounties` counter are all updated/removed as if the payout had succeeded. This exactly mirrors the Cooler `claimDefaulted` flaw: the record of an unsettled obligation (`loan.unclaimed` / the child-bounty payout) is deleted without verifying settlement occurred, permanently orphaning the funds with no on-chain reference left to recover them.

### Impact Explanation
This falls under "permanent user-fund lock" / value not conserved to the rightful beneficiary. If the transfer silently fails, the intended beneficiary and curator lose access to funds that remain locked in the derived, unreferenced `child_bounty_account`; there is no state left (`ChildBounty` entry deleted) via which a retry or recovery could be issued through the pallet.

### Likelihood Explanation
This path is reachable by any signed account (`ensure_signed(origin)?`) once the child-bounty is `PendingPayout` and the unlock block has passed — no privileged, governance, relayer, or malicious-validator assumption is required. The likelihood of an actual transfer failure depends on `T::Currency`'s behavior (e.g. interactions with holds/freezes on the bounty account, migrations, or currency backends with stricter transfer semantics), which is plausible given `debug_assert!` documents an assumption ("Should not fail") rather than a proven invariant enforced in code.

### Recommendation
Replace the `debug_assert!`-only checks with proper error propagation (e.g. `?` or explicit `ensure!`/return-on-error), so that if either transfer fails, the extrinsic returns an error and the `ChildBounty` storage entry, curator deposit, and counters are **not** mutated/removed. Only delete state and finalize the claim after confirming both transfers succeeded, consistent with the pattern used to fix the analogous Cooler `claimDefaulted` issue (settle before delete).

### Proof of Concept
1. A child-bounty reaches `ChildBountyStatus::PendingPayout` with a `curator`, `beneficiary`, and `unlock_at`.
2. Before `unlock_at` elapses (or through some external mechanism), the `child_bounty_account` ends up with a balance state that causes `T::Currency::transfer(...)` to return `Err` for the fee or payout transfer (e.g., a hold/freeze applied to the account by another interacting pallet or a currency implementation returning `Err` under specific conditions not covered by this pallet's own assumptions).
3. Any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)` once `unlock_at` has passed.
4. `fee_transfer_result` / `payout_transfer_result` are `Err`, but in a release build the `debug_assert!` checks are compiled out and do nothing.
5. Execution continues: `ParentChildBounties` count is decremented, `ChildBountyDescriptionsV1` is removed, and `*maybe_child_bounty = None` deletes the entire child-bounty record — all while the funds remain un-transferred in `child_bounty_account`, which is now orphaned with no pallet-tracked reference to it.

### Citations

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

**File:** substrate/frame/child-bounties/src/lib.rs (L754-765)
```rust
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
