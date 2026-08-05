Audit Report

## Title
Unchecked `Currency::transfer()` return value in bounty payout permanently deletes bounty state on failed transfer, permanently locking funds - (File: `substrate/frame/bounties/src/lib.rs`, function `claim_bounty`; analogous code in `substrate/frame/child-bounties/src/lib.rs`, function `impl_close_child_bounty`)

## Summary
`claim_bounty` performs the fee and payout `T::Currency::transfer()` calls from the deterministic bounty sub-account and only verifies success via `debug_assert!(res.is_ok())`, which compiles to a no-op in release builds. [1](#0-0)  The dispatchable unconditionally proceeds to clear `maybe_bounty`, remove `BountyDescriptions`, and emit `BountyClaimed` regardless of whether the transfers actually succeeded. [2](#0-1)  The identical pattern exists in `impl_close_child_bounty`, which transfers the child-bounty balance to the parent bounty account and only checks the result with `debug_assert!` before unconditionally removing the child-bounty record. [3](#0-2) 

## Finding Description
The bounty sub-account is derived deterministically and publicly computably as `T::PalletId::get().into_sub_account_truncating(("bt", id))`, meaning any actor can precompute the exact address for any (even future) bounty index. [4](#0-3)  If a transfer out of that account fails at claim time — for example due to a pre-existing balance lock placed on the account via a `LockableCurrency`-based mechanism such as `pallet_vesting::vested_transfer` targeting that address — `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` and the subsequent beneficiary transfer would return `Err(...)`. In a release build (the standard runtime build profile), `debug_assert!(res.is_ok())` is stripped out entirely, so the error is silently ignored and the code proceeds to permanently delete the bounty record and emit the `BountyClaimed` success event as if the transfer succeeded. Since `claim_bounty` is a permissionless extrinsic (`ensure_signed(origin)?; // anyone can trigger claim`) and the bounty record is deleted with no other code path to retry, any funds left un-transferred due to the failed transfer become permanently unreachable through the bounty pallet. The same structural flaw exists in `impl_close_child_bounty`, where the child→parent transfer is checked only via `debug_assert!` before the child-bounty record is unconditionally removed.

Existing guards are insufficient: the code contains only a debug-mode assertion, not a hard error return via `?`, so there is no production-time enforcement that the transfer actually completed before storage is mutated and the success event is fired.

## Impact Explanation
This matches the "permanent user-fund … lock" and "duplicate settlement or payout" impact criteria in the required-impact list: on transfer failure, the bounty/child-bounty storage entry and description are deleted, the curator deposit is already unreserved, and a success event fires, but the underlying value never moved out of the (now potentially locked) sub-account. Because the record no longer exists, there is no bounty-pallet dispatchable to retry or recover the funds, resulting in a permanent lock of value in the sub-account, reachable purely via unprivileged, permissionless extrinsics (`vested_transfer` plus `claim_bounty`), with no governance or privileged-actor involvement required.

## Likelihood Explanation
The precondition chain is fully attacker-controlled and requires no privileged access: (1) the bounty sub-account address is trivially derivable off-chain ahead of time; (2) `pallet_vesting::vested_transfer` is a standard permissionless extrinsic present in runtimes that include `pallet-vesting`, and it places a `LockableCurrency` lock on the destination account for an attacker-chosen amount and schedule; (3) the attacker fully controls the locked amount, so it can be sized to exceed the bounty account's expected free/transferable balance at claim time. The `// should not fail` / `// Should not fail; child bounty account gets this balance during creation.` comments confirm the developers assumed sub-accounts could not be independently targeted by other pallets' lock-creating calls, an assumption not enforced anywhere in the code.

## Recommendation
Replace both `debug_assert!(res.is_ok())` checks in `claim_bounty` with proper error propagation (`res?`) inside the `try_mutate_exists` closure, so a failed transfer aborts the mutation and leaves the bounty record intact and retryable, rather than silently continuing to delete state. Apply the same fix to `transfer_result` in `impl_close_child_bounty`. Consider additionally guarding bounty/child-bounty sub-accounts against externally-created locks, or using a lock-aware/`Preservation`-based transfer variant that surfaces `LiquidityRestrictions` distinctly and is always checked prior to any storage mutation.

## Proof of Concept
1. Off-chain, compute `bounty_account = PalletId::into_sub_account_truncating(("bt", bounty_id))` for a bounty index expected to be approved.
2. Before/while the bounty is funded by the treasury, call `pallet_vesting::vested_transfer` (using attacker-owned funds) targeting `bounty_account` with a `locked` amount exceeding the bounty account's expected post-funding free balance, applying a `LockableCurrency` lock on that account.
3. After the bounty is approved, awarded, and the payout delay elapses, call the permissionless `claim_bounty(bounty_id)`.
4. In a release build, both `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` and `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` return `Err(LiquidityRestrictions)`, but the stripped `debug_assert!` no-ops; the extrinsic still succeeds, `Bounties::<T,I>::remove`/`BountyDescriptions::remove` execute, and `Event::BountyClaimed` fires despite no balance transfer occurring — leaving the funds permanently locked in `bounty_account` with no retry path in the pallet. A `cargo test -p pallet-bounties` unit test built with `debug-assertions = false` (matching release semantics) reproducing this lock-then-claim sequence would confirm the exact `Err` variant and silent continuation.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L808-808)
```rust
					let bounty_account = Self::bounty_account_id(bounty_id);
```

**File:** substrate/frame/bounties/src/lib.rs (L820-826)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
```

**File:** substrate/frame/bounties/src/lib.rs (L828-837)
```rust
					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
					T::ChildBountyManager::bounty_removed(bounty_id);

					Self::deposit_event(Event::<T, I>::BountyClaimed {
						index: bounty_id,
						payout,
						beneficiary,
					});
```

**File:** substrate/frame/child-bounties/src/lib.rs (L939-951)
```rust
				let balance = T::Currency::free_balance(&child_bounty_account);
				let transfer_result = T::Currency::transfer(
					&child_bounty_account,
					&parent_bounty_account,
					balance,
					AllowDeath,
				); // Should not fail; child bounty account gets this balance during creation.
				debug_assert!(transfer_result.is_ok());

				// Remove the child-bounty description.
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;
```
