## Analog Found: Forced push-payout in `pallet-bounties::claim_bounty` silently "succeeds" and deletes state even when the underlying token transfer fails

### Title
Bounty/child-bounty claim silently deletes bounty state and reports success even when the reward-token transfer to a restricted (e.g. blacklisted/frozen) beneficiary or curator fails - (File: `substrate/frame/bounties/src/lib.rs`, `substrate/frame/child-bounties/src/lib.rs`)

### Summary
The external report's core invariant is: *a forced (push-style) transfer of a possibly-blocklisted reward token must not be allowed to corrupt or advance unrelated settlement state; failure of that transfer must not cause loss/lock of the underlying funds.* `pallet-bounties::claim_bounty` and `pallet-child-bounties::claim_child_bounty` reproduce this exact anti-pattern: they push-transfer bounty value/fee to the curator and beneficiary, treat the transfer as infallible via `debug_assert!`, and unconditionally delete the bounty record and emit a success event regardless of the actual `Result` of the transfer.

### Finding Description
`T::Currency` for `pallet-bounties`/`pallet-child-bounties` is the generic `Currency` trait inherited via `pallet_treasury::Config`, not hard-wired to `pallet_balances`. Runtimes can configure the treasury/bounties currency to be backed by a `fungible`/`fungibles` adapter over an asset such as `pallet-assets`, which supports asset freezing/administrative blocking of specific accounts (functionally equivalent to the USDC blacklist scenario in the report).

In `claim_bounty`:
```rust
// substrate/frame/bounties/src/lib.rs:820-828
let res =
    T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
debug_assert!(res.is_ok());
let res =
    T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
debug_assert!(res.is_ok());

*maybe_bounty = None;
BountyDescriptions::<T, I>::remove(bounty_id);
T::ChildBountyManager::bounty_removed(bounty_id);

Self::deposit_event(Event::<T, I>::BountyClaimed { index: bounty_id, payout, beneficiary });
``` [1](#0-0) 

The identical pattern exists in `claim_child_bounty`:
```rust
// substrate/frame/child-bounties/src/lib.rs:726-763
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());
let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());
...
*maybe_child_bounty = None;
``` [2](#0-1) 

`debug_assert!` is compiled out entirely in release/production runtime builds (the only builds that matter on-chain). If either `T::Currency::transfer` call returns `Err` (because the beneficiary/curator is frozen or blocked on the configured asset, akin to a USDC blacklist), that `Err` is discarded — there is no `?`, no `ensure!`, no rollback. Execution falls straight through to: deleting the bounty record, removing its description, decrementing the parent's active-child-bounty counter, and emitting a `BountyClaimed`/`Claimed` event that asserts the payout succeeded.

This directly matches the reported bug class: the settlement state (`bounty.status` / bounty existence) is only supposed to advance after the transfer to a potentially-restricted reward token actually succeeds, per the required invariant "payout state must only advance after ... settlement succeed atomically." Here it advances unconditionally.

### Impact Explanation
Once the bounty record is deleted, the fee/payout amount that failed to transfer remains stranded in the (now untracked) `bounty_account`/`child_bounty_account`, with no more `Bounties`/`ChildBounties` storage entry pointing at it, no retry mechanism, and a public event falsely claiming the funds were paid out. This is a permanent loss/lock of treasury-derived funds (matching the "Balances ... treasury spends ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot and the "permanent user-fund ... lock" impact bucket). Unlike the newer `pallet-multi-asset-bounties`, which uses an async `Pay`/`check_status`/`retry_payment` state machine specifically to handle payment failures safely, `pallet-bounties`/`pallet-child-bounties` assume the transfer can never fail and hard-delete state on the (false) assumption of success.

### Likelihood Explanation
Low-to-moderate: it requires a runtime configuration where the treasury/bounty `Currency` is backed by an asset that can restrict specific accounts (frozen/blocked beneficiary or curator), or any other condition causing `Currency::transfer` to return an `Err` (e.g., existential-deposit edge cases interacting with `AllowDeath` semantics for exotic currency implementations). This mirrors the report's own "Likelihood: Low" rating for USDC-style blacklisting — a rare but realistic operational condition, not requiring any privileged/malicious actor; a normal claimant can be denied because of restrictions entirely outside the bounty pallet's control.

### Recommendation
Replace `debug_assert!(res.is_ok())` with proper error propagation (`res?`) so a failed transfer aborts the extrinsic before any state mutation, or migrate `pallet-bounties`/`pallet-child-bounties` to the same claim/retry pattern already implemented in `pallet-multi-asset-bounties` (`do_process_payout_payment` + `check_status`/`retry_payment`), where payment failure keeps the bounty in a recoverable `PayoutAttempted`/`RefundAttempted` state instead of deleting it.

### Proof of Concept
1. Configure a runtime where `pallet-treasury`'s (and thus `pallet-bounties`') `Currency` is a `fungible` adapter over `pallet-assets`, and the asset supports account freezing/blocking.
2. Create, fund, and award a bounty to a `beneficiary` account.
3. Freeze/block the `beneficiary` account on the underlying asset (analogous to USDC blacklisting).
4. Call `claim_bounty` — the `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` call returns `Err`, but since `debug_assert!` is a no-op in the release runtime, execution continues to `*maybe_bounty = None` and emits `BountyClaimed { payout, beneficiary }`.
5. Result: the bounty record is gone, the event claims the payout happened, but the funds remain stuck in `bounty_account`, permanently unclaimable through any pallet extrinsic.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L820-837)
```rust
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
