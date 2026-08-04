## Analysis

The Solana report's core broken invariant is: **a payout path that unconditionally advances/finalizes state before confirming that the actual token movement succeeded, allowing an account-state precondition it doesn't control to permanently strand funds.**

The closest verified local analog is in `pallet-child-bounties` (and the parent `pallet-bounties`), where the beneficiary payout on `claim_child_bounty`/`claim_bounty` is executed with `T::Currency::transfer(..., AllowDeath)` and the result is only checked via `debug_assert!`, which compiles to a no-op in production (non-debug-assertions) runtimes. The bounty record is unconditionally removed from storage regardless of whether the transfer actually succeeded.

### Title
Permanent Fund Lock in Child/Parent Bounty Claim via Unchecked Transfer Result - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`claim_child_bounty` and `claim_bounty` transfer bounty funds to a beneficiary account using `T::Currency::transfer(..., AllowDeath)`, but only assert success with `debug_assert!`, which is stripped in release/production builds. The bounty storage entry is deleted unconditionally afterward, so if the transfer fails, the funds remain stranded in the bounty sub-account with no remaining path to retrieve them.

### Finding Description
In `claim_child_bounty`: [1](#0-0) 

the payout transfer is guarded only by `debug_assert!(payout_transfer_result.is_ok())`, and immediately after (regardless of transfer outcome) the code proceeds to decrement `ParentChildBounties`, remove the description, and set `*maybe_child_bounty = None`, permanently erasing all on-chain record of the bounty and its funds.

The analogous pattern exists in the parent `pallet-bounties::claim_bounty`: [2](#0-1) 

Both use the legacy `Currency::transfer` (not `Fungible::transfer`), which for a `beneficiary` account that does not yet exist requires the transferred `payout`/`final_fee` to be at least `ExistentialDeposit`; otherwise the call returns `Err(ExistentialDeposit)` and no funds move. Since `beneficiary` is an arbitrary, unlookup-provided `AccountId` supplied by the curator at `award_bounty`/`award_child_bounty` time (with no existence or minimum-payout check), and the curator fee/value split is derived independently of ED, it's straightforward to end up with a `payout` (or `final_fee`) below the `ExistentialDeposit` for a beneficiary that has no other funds/account history — causing the transfer to silently fail while the bounty record is destroyed and the sub-account's balance becomes permanently unreachable (no code path recreates or reclaims a "claimed" bounty).

This mirrors the reported class of bug: an unprivileged party (the curator, who is not necessarily privileged/governance — any accepted curator of an already-approved bounty) can pick a beneficiary/value combination that causes the settlement transfer to fail, while the pallet nonetheless advances state as if settlement succeeded, permanently locking the funds instead of returning them to the treasury or leaving the bounty claimable.

### Impact Explanation
This directly violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after ... settlement succeed[s] atomically," and results in "permanent user-fund lock" — an explicitly in-scope impact. Bounty/treasury funds become unrecoverable: they remain in `bounty_account_id`/`child_bounty_account_id`, which is a keyless derived account with no extrinsic capable of sweeping it back (unlike `reclaim_bounty_funds`, which only operates on *closed* bounties, not ones removed via a failed claim).

### Likelihood Explanation
Requires only a normal, non-privileged curator role (already reachable via the standard bounty curator-acceptance flow, not governance) choosing (or being handed, e.g. through a malicious/careless beneficiary submission) a beneficiary account with zero balance and a computed payout below `ExistentialDeposit`. This is easily engineered by controlling the child-bounty `value`/`fee` split or by the parent bounty's fee/value being small. `debug_assert!` being compiled out in production builds is standard for polkadot-sdk release runtimes, so the guard provides no actual protection on-chain.

### Recommendation
- Replace `debug_assert!(...is_ok())` with actual `?`/`ensure!` error propagation so the extrinsic fails atomically if the transfer does not succeed, instead of silently finalizing state.
- Alternatively, use `Preservation`-aware `Fungible::transfer` with explicit handling for sub-ED payouts (e.g., top up to ED from the bounty pot, or reject `award_bounty`/`award_child_bounty` when the beneficiary/value combination cannot satisfy ED, or fall back to `mint_creating`-style semantics as staking's `asset::mint_creating` does).
- Ensure any failure path leaves the bounty in a retryable/reclaimable status rather than deleting the record.

### Proof of Concept
1. Curator calls `award_child_bounty` with `beneficiary` = a brand-new account with zero balance, and a bounty `value`/`fee` split such that `payout = balance.saturating_sub(curator_fee)` is nonzero but less than `ExistentialDeposit`.
2. After `unlock_at`, anyone calls `claim_child_bounty`.
3. `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err(ExistentialDeposit)` because `beneficiary` doesn't exist and `payout < ED`; in a release build `debug_assert!` performs no check.
4. Execution continues: `ParentChildBounties` decremented, `ChildBountyDescriptionsV1` removed, `*maybe_child_bounty = None`.
5. `child_bounty_account`'s remaining balance (the failed `payout`) is now permanently stranded — no extrinsic references this bounty or its funds anymore. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L686-771)
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
		}
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
