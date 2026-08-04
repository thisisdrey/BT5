### Title
Bounty and child-bounty payout transfers rely on `debug_assert!` instead of checked results, allowing state to advance to "Claimed"/removed even when the payout transfer silently fails - ([File: substrate/frame/child-bounties/src/lib.rs])

### Summary
`claim_child_bounty()` in `pallet-child-bounties` and `claim_bounty()` in `pallet-bounties` both perform the curator-fee and beneficiary payout transfers, but only verify the `Result` of these currency transfers with `debug_assert!`, not with `?` or an explicit `ensure!`/error return. `debug_assert!` is compiled out in release builds (the profile used for production runtime binaries), so any failure of the transfer is silently ignored, exactly mirroring the reported pattern of "unchecked low-level call" where a fund-moving call's success is never verified before the caller proceeds to record the payment as complete.

### Finding Description
In `claim_child_bounty()`: [1](#0-0) 
the curator-fee transfer and beneficiary transfer are performed with `T::Currency::transfer(...)`, and their `DispatchResult`s are checked only via `debug_assert!(fee_transfer_result.is_ok())` and `debug_assert!(payout_transfer_result.is_ok())`. Regardless of whether these transfers actually succeeded, the function unconditionally proceeds to: [2](#0-1) 
- emit `Event::Claimed` with the computed `payout` amount and `beneficiary`,
- decrement `ParentChildBounties` count,
- remove the child-bounty description,
- set `*maybe_child_bounty = None`, permanently deleting the on-chain record of the child bounty.

The identical pattern exists in the parent pallet's `claim_bounty()`: [3](#0-2) 
where `err_amount.is_zero()` and `res.is_ok()` are checked with `debug_assert!` before the bounty record is deleted (`*maybe_bounty = None`) and the `BountyClaimed` event is emitted.

`debug_assert!` macros expand to a no-op unless `cfg(debug_assertions)` is enabled, which is off in standard release compilation profiles used to build the runtime. No `#[cfg]` override forcing debug-assertions on for these runtime crates was found in the workspace `Cargo.toml`. Consequently, in a production build, if `T::Currency::transfer` returns an `Err` (e.g., due to unexpected existence-requirement / dust / hold interactions on the `AllowDeath` transfer, or a `Currency` implementation edge case not anticipated by the "should not fail" comment), the corrupted invariant is: **the bounty's completion state (`Claimed`/`BountyClaimed` event + storage removal) is committed even though the actual balance movement to the beneficiary and/or curator did not occur.**

This differs from typical guarded transfers elsewhere in the codebase (e.g., `substrate/frame/revive/src/exec.rs` uses `?` to propagate `Error::<T>::TransferFailed`, and `substrate/frame/multi-asset-bounties/src/lib.rs` propagates `Error::<T, I>::PayoutError` via `map_err`), showing that the checked-transfer pattern is the norm and this pair of call paths is the outlier.

### Impact Explanation
If a transfer fails after all the guard checks pass, funds remain stuck in the `child_bounty_account_id` / `bounty_account_id` sovereign account, which is disassociated from any pallet storage once the bounty record is deleted — no other code path can be used to recover the funds because the bounty index has been reaped. This is a "permanent user-fund lock" and "duplicate/incorrect settlement" scenario under the required impact set: the payout is recorded as delivered to the rightful beneficiary (event + reduced counters) while no value actually moved, breaking the invariant that settlement state should only advance after the payout genuinely succeeds.

### Likelihood Explanation
Likelihood is Low: `Currency::transfer` for the crate's typical `pallet-balances` implementation using `AllowDeath`/`Preservation::Expendable` rarely errors for a well-formed transfer with sufficient balance, and both call sites compute `curator_fee`/`payout` from the account's own free balance, reducing but not eliminating the chance of failure (e.g., interplay with holds/freezes present on the bounty sub-account, or a non-standard `T::Currency` implementation configured by a runtime). Because this only requires an ordinary signed caller invoking the already-permissionless `claim_bounty`/`claim_child_bounty` extrinsics, no privileged or malicious-actor assumption is needed to trigger the path if the transfer preconditions are ever violated.

### Recommendation
Replace `debug_assert!(result.is_ok())` with proper error propagation (`?` or `ensure!`) in both `claim_bounty()` (`substrate/frame/bounties/src/lib.rs`) and `claim_child_bounty()` (`substrate/frame/child-bounties/src/lib.rs`), so that a failed currency transfer aborts the extrinsic (reverting the `try_mutate_exists` closure) instead of allowing the bounty to be marked claimed/removed. This mirrors the pattern already used in `pallet-revive`'s `transfer()` and `pallet-multi-asset-bounties`'s `do_process_payout_payment()`.

### Proof of Concept
1. Configure a runtime (built in release/production mode, i.e. `cfg(debug_assertions)` disabled) with a `pallet-child-bounties::Config::Currency` whose `transfer` can return `Err` for the specific beneficiary/amount combination reached by `claim_child_bounty` (e.g., a beneficiary account subject to a `Freeze`/`Hold` that rejects the incoming credit under the given `Preservation`, or a custom `Currency` adapter with a narrow failure condition not covered by the "should not fail" assumptions).
2. Fund and progress a child bounty through `add_child_bounty` → `propose_curator` → `accept_curator` → `award_child_bounty` to reach `ChildBountyStatus::PendingPayout`.
3. Wait for `unlock_at`, then call `claim_child_bounty(origin, parent_bounty_id, child_bounty_id)` as any signed account.
4. Because the build has `debug_assertions` disabled, `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` failing has no effect on control flow — the function still emits `Event::Claimed`, decrements `ParentChildBounties`, deletes `ChildBountyDescriptionsV1`, and sets `*maybe_child_bounty = None`.
5. Observe: `payout` amount remains in `child_bounty_account_id`, the beneficiary received nothing, yet the chain state and events report the bounty as successfully claimed and paid, with no remaining bounty record to retry or recover the stuck funds.

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

**File:** substrate/frame/bounties/src/lib.rs (L812-828)
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
```
