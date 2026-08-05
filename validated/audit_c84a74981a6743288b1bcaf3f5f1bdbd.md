Audit Report

## Title
Silent transfer failure in bounty payout permanently locks funds in `claim_bounty`/`claim_child_bounty` - (File: `substrate/frame/bounties/src/lib.rs`, `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`claim_bounty` and `claim_child_bounty` perform two sequential `T::Currency::transfer(..., AllowDeath)` calls to pay the curator fee and the beneficiary payout, but only check the result with `debug_assert!`, which is compiled out in release/production builds. If either transfer fails (e.g., the destination account does not exist and the transferred amount is below the Existential Deposit), the dispatchable still returns `Ok(())`, emits a success event, and unconditionally deletes the bounty/child-bounty storage entry, permanently stranding the undelivered funds in the bounty's derived sovereign account with no remaining code path to reclaim them.

## Finding Description
In `claim_bounty`, after computing `fee`/`payout` from the bounty account's balance, the code executes: [1](#0-0) 
Both `T::Currency::transfer` calls are checked only via `debug_assert!(res.is_ok())`, not `?` or `ensure!`, so a `DispatchError` returned by either transfer is silently discarded in release builds. Immediately after, the bounty record is unconditionally cleared regardless of whether the transfers succeeded: [2](#0-1) 

The identical pattern exists in `claim_child_bounty`: [3](#0-2) 

`pallet_balances`'s `transfer` with `AllowDeath` fails when the destination account does not exist and the transferred amount is below `ExistentialDeposit` — an ordinary, unprivileged condition (e.g., a fresh beneficiary account and a small residual payout), not requiring any malicious or privileged actor. Because the check is `debug_assert!` rather than propagated error handling, this failure is invisible in a release-mode runtime: the extrinsic returns `Ok(())`, the `BountyClaimed`/`Claimed` event fires as if payment succeeded, and the bounty/child-bounty storage entry referencing the derived account is deleted — removing the only remaining pointer to reclaim the stranded balance.

## Impact Explanation
Once the bounty/child-bounty entry is removed, there is no on-chain reference to the derived sovereign account (`bounty_account_id`/`child_bounty_account_id`) holding the un-transferred funds, and no retry dispatchable exists for this specific case. This matches the "permanent user-fund lock" category in the impact gate: funds become permanently unreachable through any exposed extrinsic, with the exact corrupted state being the deleted `Bounties::<T,I>` (or `ChildBounties::<T>`) entry and the un-drained balance left in the bounty's sovereign account.

## Likelihood Explanation
Triggering requires the final `payout` or `final_fee` to fall below `ExistentialDeposit` while the recipient account has zero prior balance — realistic for freshly generated beneficiary/curator accounts or very small bounty/fee amounts. A curator, who is an ordinary unprivileged (though assigned) actor within the bounty workflow, can engineer this by proposing a fee close to the full bounty value, leaving a sub-ED beneficiary payout. `debug_assert!` masking this in release builds while catching it in debug/test builds explains why it plausibly evaded testing. This does not require any malicious peer, validator, collator, or off-repo compromise — it is reachable purely through the standard `propose_bounty` → `approve_bounty` → `propose_curator` → `accept_curator` → `award_bounty` → `claim_bounty` public extrinsic flow.

## Recommendation
Replace `debug_assert!(res.is_ok())` / `debug_assert!(fee_transfer_result.is_ok())` / `debug_assert!(payout_transfer_result.is_ok())` with real error propagation (`?`), and do not clear the bounty/child-bounty record (`*maybe_bounty = None`, `*maybe_child_bounty = None`, description removal, counter decrements) until all transfers have been confirmed successful. Alternatively, adopt a retryable per-recipient payment-state pattern (as used elsewhere in the codebase for asset bounties) so that a transient/ED-related transfer failure does not destroy claim state and can be retried independently for each recipient.

## Proof of Concept
1. Propose and fund a bounty with value `V` via `propose_bounty` + `approve_bounty`.
2. Call `propose_curator` with `fee = V - 1` (or any fee such that `V - fee < ExistentialDeposit`).
3. Call `accept_curator`, then `award_bounty(curator, beneficiary)` where `beneficiary` is a brand-new account with zero balance.
4. After the payout delay elapses, call `claim_bounty(bounty_id)` in a release-mode build (`debug_assertions` disabled).
5. Observe: `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)` returns `Err` (destination doesn't exist, amount below ED); the `debug_assert!` is compiled out so execution continues, `BountyClaimed` fires, and the `Bounties::<T,I>` entry is removed.
6. Verify: beneficiary's free balance remains 0; `Balances::free_balance(bounty_account_id(bounty_id))` still holds the undelivered `payout`; `Bounties::<T,I>::get(bounty_id)` returns `None` — funds are now permanently unreachable via any exposed dispatchable. [4](#0-3) [5](#0-4)

### Citations

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

**File:** substrate/frame/child-bounties/src/lib.rs (L686-770)
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
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)
```
