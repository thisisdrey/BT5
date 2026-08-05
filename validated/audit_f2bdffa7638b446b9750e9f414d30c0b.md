Audit Report

## Title
Bounty claim silently loses payout funds when beneficiary transfer fails, since the result is only checked via `debug_assert!` - (File: `substrate/frame/bounties/src/lib.rs`)

## Summary
`claim_bounty` transfers the curator fee and the remaining `payout` to the curator-chosen `beneficiary`, but checks the outcome of both `T::Currency::transfer` calls only with `debug_assert!(res.is_ok())`, which is stripped out in release builds. If the transfer to `beneficiary` fails (e.g., because `payout` is below the chain's existential deposit and `beneficiary` is an unfunded/new account), the pallet still deletes the bounty record and emits `BountyClaimed` as if the payout succeeded, permanently stranding funds in the bounty account with no remaining call path to recover them.

## Finding Description
`award_bounty` lets the curator (an unprivileged, non-governance role reached via `accept_curator`) freely pick `beneficiary` and effectively controls `fee` (set at proposal time, up to `value`), transitioning the bounty into `BountyStatus::PendingPayout`. [1](#0-0) 

`claim_bounty` is callable by any signed account (not just curator/beneficiary), computes `payout = balance.saturating_sub(fee)`, and performs:

```rust
let res = T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath);
debug_assert!(res.is_ok());
let res = T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath);
debug_assert!(res.is_ok());

*maybe_bounty = None;
BountyDescriptions::<T, I>::remove(bounty_id);
T::ChildBountyManager::bounty_removed(bounty_id);
Self::deposit_event(Event::<T, I>::BountyClaimed { index: bounty_id, payout, beneficiary });
``` [2](#0-1) 

`debug_assert!` is a no-op in release/production builds, so any `Err` returned by the transfer to `beneficiary` (e.g., insufficient existential deposit for a never-funded destination account) is silently discarded. Execution proceeds identically to the success case: the bounty entry is removed via `*maybe_bounty = None`, `BountyDescriptions` is removed, `bounty_removed` is invoked, and `BountyClaimed { payout, beneficiary }` is emitted — falsely reporting settlement. Since no other extrinsic references a removed `bounty_id` (award/claim/close all require `Bounties::<T,I>::get`), the `payout` amount left in `bounty_account_id(bounty_id)` becomes permanently unreachable through pallet logic. No existing guard in `propose_bounty` or elsewhere in the file was found (via grep) that enforces a minimum `payout` (post-fee) against the existential deposit, so this path is not otherwise mitigated.

## Impact Explanation
This matches the "permanent user-fund lock" and "duplicate/false settlement" impact categories: bounty funds become permanently stuck in the bounty sub-account while on-chain state (event log + absent bounty record) falsely represents the payout as completed, with no code path to recover or re-trigger it.

## Likelihood Explanation
The trigger requires only an accepted curator (an ordinary, ungated role) choosing an unfunded `beneficiary` and a `fee` close to `value`, followed by any signed account calling `claim_bounty` after the unlock delay — no privileged governance, validator, or off-chain compromise is needed. It can also occur unintentionally whenever a legitimately-awarded beneficiary happens to be unfunded and the residual payout is below the existential deposit, making it both attacker-triggerable and accident-prone in production (release) builds where `debug_assert!` is compiled out.

## Recommendation
Replace both `debug_assert!(res.is_ok())` checks in `claim_bounty` with `ensure!`/`?` error propagation so a failed transfer aborts the extrinsic and leaves the bounty in `PendingPayout` state and funds in the bounty account, rather than deleting the bounty and reporting false success. Apply the same fix to the analogous `claim_child_bounty` logic in `substrate/frame/child-bounties/src/lib.rs`. Additionally, consider validating that `payout` and `final_fee` each meet the existential deposit (or use keep-alive-aware balance checks) before finalizing state.

## Proof of Concept
1. On a chain with `ExistentialDeposit = E > 0`, propose and fund a bounty with `value = V`, with `fee` set close to `V` at proposal.
2. Curator accepts curatorship via `accept_curator`, then calls `award_bounty(bounty_id, beneficiary)` with `beneficiary` a brand-new, never-funded account, such that `payout = value - fee < E`.
3. Wait `BountyDepositPayoutDelay` blocks.
4. Any signed account calls `claim_bounty(bounty_id)`.
5. In a release build: the transfer to `curator` succeeds; the transfer to `beneficiary` fails internally (`payout < E`, destination account doesn't exist), but `debug_assert!(res.is_ok())` discards the error.
6. `Bounties::<T, I>` entry is removed and `BountyClaimed { payout, beneficiary }` is emitted despite the failed transfer, leaving `payout` permanently stranded in `bounty_account_id(bounty_id)`.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L750-783)
```rust
		pub fn award_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			Bounties::<T, I>::try_mutate_exists(bounty_id, |maybe_bounty| -> DispatchResult {
				let bounty = maybe_bounty.as_mut().ok_or(Error::<T, I>::InvalidIndex)?;

				// Ensure no active child bounties before processing the call.
				ensure!(
					T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
					Error::<T, I>::HasActiveChildBounty
				);

				match &bounty.status {
					BountyStatus::Active { curator, .. } => {
						ensure!(signer == *curator, Error::<T, I>::RequireCurator);
					},
					_ => return Err(Error::<T, I>::UnexpectedStatus.into()),
				}
				bounty.status = BountyStatus::PendingPayout {
					curator: signer,
					beneficiary: beneficiary.clone(),
					unlock_at: Self::treasury_block_number() + T::BountyDepositPayoutDelay::get(),
				};

				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::BountyAwarded { index: bounty_id, beneficiary });
			Ok(())
```

**File:** substrate/frame/bounties/src/lib.rs (L796-844)
```rust
		pub fn claim_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResult {
			ensure_signed(origin)?; // anyone can trigger claim

			Bounties::<T, I>::try_mutate_exists(bounty_id, |maybe_bounty| -> DispatchResult {
				let bounty = maybe_bounty.take().ok_or(Error::<T, I>::InvalidIndex)?;
				if let BountyStatus::PendingPayout { curator, beneficiary, unlock_at } =
					bounty.status
				{
					ensure!(Self::treasury_block_number() >= unlock_at, Error::<T, I>::Premature);
					let bounty_account = Self::bounty_account_id(bounty_id);
					let balance = T::Currency::free_balance(&bounty_account);
					let fee = bounty.fee.min(balance); // just to be safe
					let payout = balance.saturating_sub(fee);
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
					Ok(())
				} else {
					Err(Error::<T, I>::UnexpectedStatus.into())
				}
			})?;
			Ok(())
		}
```
