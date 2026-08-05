Audit Report

## Title
Unchecked `Currency::transfer` return value in `claim_child_bounty` permanently locks payout funds and emits a false success event - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`claim_child_bounty` performs curator-fee and beneficiary payout transfers via `T::Currency::transfer(..., AllowDeath)` and only guards the result with `debug_assert!`, which is compiled out in release builds. If a transfer fails (e.g., destination below `ExistentialDeposit`), the pallet still deletes the child-bounty record and emits `Event::Claimed` as if the payout succeeded, permanently stranding funds with no recovery path.

## Finding Description
In `claim_child_bounty`, after computing `curator_fee` and `payout` from the child-bounty sub-account's free balance [1](#0-0) , both value-moving transfers are checked only with `debug_assert!(...is_ok())`, not `?` or any branching logic [2](#0-1) . `debug_assert!` is a no-op in `--release` builds, which is what production runtimes use, so a failed transfer is silently ignored. Execution then unconditionally proceeds to emit `Event::Claimed`, decrement `ParentChildBounties`, remove the description, and set `*maybe_child_bounty = None`, deleting the only on-chain record of the child bounty [3](#0-2) .

Once the entry is removed, there is no recovery: `impl_close_child_bounty` requires an existing `ChildBounties` entry (`ok_or(BountiesError::<T>::InvalidIndex)?`) and explicitly refuses to close a bounty in `PendingPayout` state anyway [4](#0-3) ; after the claim the map entry doesn't exist at all, so any later `close_child_bounty` call fails with `Error::InvalidIndex`. The identical unchecked-transfer pattern also exists in the parent `claim_bounty` in `pallet-bounties` [5](#0-4) .

`claim_child_bounty` is callable by any signed account (`ensure_signed(origin)?`) once the child bounty is in `PendingPayout`, with no further authorization check — the caller need not be the beneficiary, curator, or any privileged party.

## Impact Explanation
This matches the "permanent user-fund lock" and "runtime bug compromising intended behavior" impact categories. If the beneficiary transfer fails, funds remain stuck in the child-bounty sub-account with no dispatchable capable of referencing the now-deleted index to reclaim or redirect them, while the emitted `Claimed` event and removed storage falsely represent the payout as completed — a state/event integrity break paired with a fund-lock condition.

## Likelihood Explanation
The scenario is attacker/user reachable without any privileged actor: the child-bounty curator (via `award_child_bounty`) can set the beneficiary to an arbitrary account address supplied at award time, and small `value` child bounties (below `ExistentialDeposit`) combined with a fresh/never-funded beneficiary account are a realistic, unprivileged trigger for `pallet_balances::transfer` returning `Err` on account creation under ED rules. No governance, relayer, or validator compromise is required — only ordinary use of the public `award_child_bounty`/`claim_child_bounty` extrinsics. I was not able to fully verify from the code reviewed whether `add_child_bounty` or `award_child_bounty` enforce any minimum-value constraint tied to `ExistentialDeposit` that would categorically prevent sub-ED payouts (the `add_child_bounty` snippet reviewed shows only a parent-balance sufficiency check via `ensure_can_withdraw`, not an ED floor on `value`), so this should be confirmed with a concrete runtime configuration/test before treating likelihood as fully established.

## Recommendation
Propagate the `DispatchResult` of both `Currency::transfer` calls with `?` (or explicit fallback handling, e.g., redirecting undeliverable dust to the treasury) before mutating/removing the `ChildBounties` entry and before emitting `Event::Claimed`. Apply the same fix to the analogous unchecked transfers in `pallet-bounties::claim_bounty`.

## Proof of Concept
1. Parent bounty curator creates and funds a child bounty via `add_child_bounty` with `value` set below the runtime's `ExistentialDeposit`.
2. Curator is proposed/accepted, and `award_child_bounty` sets `beneficiary` to a fresh account that has never held a balance.
3. After the unlock delay, any signed account calls `claim_child_bounty(parent_bounty_id, child_bounty_id)`.
4. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err` because `payout < ExistentialDeposit` and the destination doesn't exist; `debug_assert!` is a no-op in release, so execution continues.
5. `Event::Claimed` fires and `*maybe_child_bounty = None` removes the record; a subsequent `close_child_bounty` call returns `Error::InvalidIndex`, and the funds remain stuck in `child_bounty_account_id(parent_bounty_id, child_bounty_id)` permanently. A Rust integration test in `substrate/frame/child-bounties/src/tests.rs` built in release mode (or using `should_panic`-free assertions instead of `debug_assert!`) reproducing this scenario would confirm the funds-locked/false-event state.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L714-724)
```rust
						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);
```

**File:** substrate/frame/child-bounties/src/lib.rs (L726-763)
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
```

**File:** substrate/frame/child-bounties/src/lib.rs (L894-922)
```rust
	fn impl_close_child_bounty(
		parent_bounty_id: BountyIndex,
		child_bounty_id: BountyIndex,
	) -> DispatchResult {
		ChildBounties::<T>::try_mutate_exists(
			parent_bounty_id,
			child_bounty_id,
			|maybe_child_bounty| -> DispatchResult {
				let child_bounty =
					maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

				match &child_bounty.status {
					ChildBountyStatus::Added | ChildBountyStatus::CuratorProposed { .. } => {
						// Nothing extra to do besides the removal of the child-bounty below.
					},
					ChildBountyStatus::Active { curator } => {
						// Cancelled by parent curator or RejectOrigin,
						// refund deposit of the working child-bounty curator.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);
						// Then execute removal of the child-bounty below.
					},
					ChildBountyStatus::PendingPayout { .. } => {
						// Child-bounty is already in pending payout. If parent
						// curator or RejectOrigin wants to close this
						// child-bounty, it should mean the child-bounty curator
						// was acting maliciously. So first unassign the
						// child-bounty curator, slashing their deposit.
						return Err(BountiesError::<T>::PendingPayout.into());
					},
```

**File:** substrate/frame/bounties/src/lib.rs (L820-827)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

```
