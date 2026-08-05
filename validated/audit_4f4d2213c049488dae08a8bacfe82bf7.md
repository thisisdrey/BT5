Confirmed the code matches the claim exactly — both `claim_bounty` in `substrate/frame/bounties/src/lib.rs` and `claim_child_bounty` in `substrate/frame/child-bounties/src/lib.rs` guard fund transfers only with `debug_assert!`, which is stripped in release builds, then unconditionally finalize state.Audit Report

## Title
Unchecked `debug_assert!`-guarded fund transfers in `claim_bounty`/`claim_child_bounty` allow bounty state finalization and success events without actual payout, permanently locking funds - ([File: substrate/frame/bounties/src/lib.rs], [File: substrate/frame/child-bounties/src/lib.rs])

## Summary
`pallet_bounties::Pallet::claim_bounty` and `pallet_child_bounties::Pallet::claim_child_bounty` transfer the curator fee and beneficiary payout from the deterministic bounty/child-bounty account using `T::Currency::transfer(..., AllowDeath)`, but only check the result with `debug_assert!`, which compiles to a no-op in release builds. If either transfer fails (e.g., due to a lock/freeze placed on the bounty account's balance), both functions unconditionally proceed to delete the bounty/child-bounty storage record and emit a `BountyClaimed`/`Claimed` event carrying the full intended payout, even though funds were never moved.

## Finding Description
In `claim_bounty`, `substrate/frame/bounties/src/lib.rs` L820-826, `final_fee` and `payout` are computed from the bounty account's free balance, then two `T::Currency::transfer` calls move funds to the curator and beneficiary respectively, each followed only by `debug_assert!(res.is_ok())`: [1](#0-0) 
Immediately after, regardless of the transfer outcome, the code sets `*maybe_bounty = None`, removes `BountyDescriptions`, notifies `T::ChildBountyManager::bounty_removed`, and emits `Event::BountyClaimed { index: bounty_id, payout, beneficiary }`: [2](#0-1) 

The equivalent pattern exists in `claim_child_bounty`, `substrate/frame/child-bounties/src/lib.rs` L714-744, where `fee_transfer_result` and `payout_transfer_result` are only checked via `debug_assert!`: [3](#0-2) 
followed by unconditional event emission and storage removal: [4](#0-3) 

`debug_assert!` is stripped from release/production builds (the standard build profile for runtime WASM), so a failed transfer is silently ignored. `T::Currency::transfer` with `Preservation::AllowDeath` (`AllowDeath`) only relaxes the existential-deposit requirement; it does not bypass `Lock`/`Freeze` restrictions imposed by other pallets (e.g., `pallet_vesting`, `pallet_democracy` conviction locks) on the source account. Both `bounty_account_id(bounty_id)` and `child_bounty_account_id(parent_id, child_id)` are deterministic addresses derivable off-chain from a public `PalletId` and index, so any unprivileged account can pre-position a lock on that exact account (e.g. via `pallet_vesting::vested_transfer`) before the claim executes, causing the transfer to fail with `Err` while the calling code proceeds as if it succeeded.

## Impact Explanation
This violates the required invariant that payout state may only advance after settlement succeeds atomically. If exploited, the bounty/child-bounty storage entry is deleted so the pallet loses all further ability to reference the stuck account, permanently locking the treasury funds inside an orphaned derived account — a permanent user-fund lock. Simultaneously, a `BountyClaimed`/`Claimed` event is emitted attesting to a payout that never occurred, misleading any off-chain indexer, treasury accounting system, or downstream automation into believing settlement happened. No privileged role is required to trigger the failure condition or to call the permissionless claim extrinsics.

## Likelihood Explanation
Moderate-to-high: bounty account addresses are trivially computable off-chain from public `PalletId` values and sequential indices, `pallet_vesting::vested_transfer` (or other lock-adding calls) is a standard permissionless extrinsic, and both `claim_bounty` and `claim_child_bounty` require only `ensure_signed` — no privileged access, timing race beyond ordinary transaction ordering, or validator/relayer compromise is needed.

## Recommendation
Replace the `debug_assert!(res.is_ok())` / `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())` checks with real error propagation (`?` or `ensure!`) so a failed transfer aborts the extrinsic before storage removal and event emission. Consider emitting the actually-transferred amount rather than the pre-computed intended amount, and/or hardening the derived bounty accounts against external lock-placement (e.g., by using `Preservation::Expendable`/checked withdrawal patterns that surface lock-related failures before mutating state).

## Proof of Concept
1. Attacker computes `bounty_account_id(bounty_id)` (pure function of `PalletId` + index) off-chain.
2. Before the bounty enters/while it is in `PendingPayout`, attacker calls `pallet_vesting::vested_transfer` targeting that derived account with a long vesting schedule, placing a `Lock`/`Freeze` on part of its balance.
3. After the payout delay, anyone calls `claim_bounty(bounty_id)`.
4. `T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath)` and/or the beneficiary transfer fail with `Err(LiquidityRestrictions)` due to the lock; `debug_assert!(res.is_ok())` is a no-op in release builds.
5. `*maybe_bounty = None`, `BountyDescriptions` removed, and `Event::BountyClaimed { payout, beneficiary }` emitted despite no funds moving — reproducible as a unit test built in release/non-debug-assertions mode by placing a lock on the computed bounty account and asserting the account balance is unchanged after `claim_bounty` while the event and storage indicate success.

### Citations

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

**File:** substrate/frame/bounties/src/lib.rs (L828-838)
```rust
					*maybe_bounty = None;

					BountyDescriptions::<T, I>::remove(bounty_id);
					T::ChildBountyManager::bounty_removed(bounty_id);

					Self::deposit_event(Event::<T, I>::BountyClaimed {
						index: bounty_id,
						payout,
						beneficiary,
					});
					Ok(())
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

**File:** substrate/frame/child-bounties/src/lib.rs (L746-765)
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

						Ok(())
```
