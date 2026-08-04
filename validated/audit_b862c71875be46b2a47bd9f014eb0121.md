## Analysis

The Origin Marketplace bug is about a public-facing dispatch (`makeOffer`) accepting an attacker-chosen "commission" destination address with no check against the protocol's own account, so that on payout the funds settle into an unreachable pot instead of a real beneficiary and are permanently stuck.

The closest structural analog in this repo is in `pallet-child-bounties`: `award_child_bounty` lets the signer (the current curator, which can also be the parent curator) pick an arbitrary `beneficiary` account with **no validation** against known keyless/protocol-owned accounts, and `claim_child_bounty` then unconditionally transfers the payout to that `beneficiary` and immediately deletes the child-bounty's storage entry. [1](#0-0) 

If `beneficiary` is set to the child-bounty's own keyless sub-account (`Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)`), `claim_child_bounty` performs a self-transfer (a no-op that always succeeds under `pallet_balances`) and then removes the bounty record from storage: [2](#0-1) 

Unlike `pallet-bounties`, which recently gained a permissionless `reclaim_bounty_funds` extrinsic specifically to sweep funds stranded in a closed bounty's account back to the treasury (fixing exactly this class of "funds stuck in a keyless derived account" bug): [3](#0-2) [4](#0-3) 

`pallet-child-bounties` has **no equivalent reclaim mechanism** — there is no `reclaim_child_bounty_funds`/similar call, so once the child-bounty entry is removed from `ChildBounties` storage, the balance held in `child_bounty_account_id(parent_bounty_id, child_bounty_id)` is permanently unreachable by any extrinsic.

### Title
Child-bounty curator can self-award to the bounty's own keyless account, permanently stranding funds - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`award_child_bounty` accepts an arbitrary `beneficiary` account from the curator without checking it against the child-bounty's own derived (keyless) sub-account. `claim_child_bounty` then transfers the payout to that beneficiary and deletes the child-bounty's storage entry unconditionally. If the beneficiary equals `child_bounty_account_id(parent_bounty_id, child_bounty_id)`, the transfer becomes a self-transfer no-op, and the pallet has no reclaim mechanism (unlike `pallet-bounties`'s `reclaim_bounty_funds`) to recover the now-orphaned balance.

### Finding Description
`award_child_bounty` takes a signer-supplied `beneficiary: AccountIdLookupOf<T>` with the only checks being that the signer is the curator/parent curator and the child-bounty is `Active`: [5](#0-4) 

There is no validation that `beneficiary` differs from `Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)` (or the parent bounty account, or treasury account). `claim_child_bounty` computes `payout` from the child-bounty's actual account balance and transfers it to whatever `beneficiary` was recorded, then unconditionally clears the storage entry: [2](#0-1) 

Because `T::Currency::transfer` from an account to itself is a valid no-op in `pallet_balances`, the call succeeds, the `Claimed` event fires, and `*maybe_child_bounty = None` removes all bookkeeping — including the size tracking that would otherwise identify the account as holding a live child-bounty balance. Crucially, `pallet-child-bounties` has no analog to the `reclaim_bounty_funds` extrinsic that `pallet-bounties` added (PR #11045) specifically to sweep stranded balances in closed bounty accounts back to the treasury, so once a child-bounty is closed via a self-referential beneficiary, its funds are permanently orphaned with no code path to recover them.

### Impact Explanation
This causes permanent lock of the bounty value (which originates from the treasury via the parent bounty), matching the "permanent user-fund or bridge-state lock" impact category. The amount is bounded by the child-bounty's `value`/`fee`, which can be set arbitrarily large by governance/council when creating child bounties, so the loss magnitude scales with treasury allocation to bounties.

### Likelihood Explanation
Exploitation requires only that the child-bounty's curator (a role that can be assigned to any account accepted via `accept_curator`, not necessarily a highly trusted governance actor) call `award_child_bounty` with a computed `beneficiary` equal to the deterministic, publicly-derivable `child_bounty_account_id`. No special privilege beyond being curator of that specific child bounty is needed, and the derivation function is public (`Pallet::<T>::child_bounty_account_id`), so any curator can trivially compute the target address.

### Recommendation
- **Short term:** In `award_child_bounty`, reject `beneficiary` values equal to `Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)`, the parent bounty account, or the treasury/pallet account.
- **Long term:** Add a permissionless reclaim/sweep extrinsic to `pallet-child-bounties`, mirroring `pallet-bounties::reclaim_bounty_funds`, that can recover any balance stranded in a closed child-bounty's derived account.

### Proof of Concept
1. Governance creates a parent bounty and funds it; a curator is accepted for the parent bounty.
2. Parent curator adds a child bounty (`add_child_bounty`) with some `value`; a curator is proposed and accepts (`accept_curator`) for the child bounty. The child-bounty's derived account `child_bounty_account_id(parent_id, child_id)` now holds `value` (plus fee).
3. The child-bounty curator computes `beneficiary = child_bounty_account_id(parent_id, child_id)` and calls `award_child_bounty(origin, parent_id, child_id, beneficiary)`.
4. Any signed account calls `claim_child_bounty(origin, parent_id, child_id)` after the payout delay.
5. `claim_child_bounty` transfers `payout` from `child_bounty_account` to itself (no-op) and removes the `ChildBounties` entry — the funds remain in `child_bounty_account`, but there is no longer any storage record or extrinsic that references or can withdraw from this account, permanently stranding the funds.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L617-656)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(<T as Config>::WeightInfo::award_child_bounty())]
		pub fn award_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
			beneficiary: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::Lookup::lookup(beneficiary)?;

			// Ensure parent bounty exists, and is active.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ChildBounties::<T>::try_mutate_exists(
				parent_bounty_id,
				child_bounty_id,
				|maybe_child_bounty| -> DispatchResult {
					let child_bounty =
						maybe_child_bounty.as_mut().ok_or(BountiesError::<T>::InvalidIndex)?;

					// Ensure child-bounty is in active state.
					if let ChildBountyStatus::Active { ref curator } = child_bounty.status {
						ensure!(
							signer == *curator || signer == parent_curator,
							BountiesError::<T>::RequireCurator,
						);
						// Move the child-bounty state to pending payout.
						child_bounty.status = ChildBountyStatus::PendingPayout {
							curator: signer,
							beneficiary: beneficiary.clone(),
							unlock_at: Self::treasury_block_number() +
								T::BountyDepositPayoutDelay::get(),
						};
						Ok(())
					} else {
						Err(BountiesError::<T>::UnexpectedStatus.into())
					}
				},
			)?;
```

**File:** substrate/frame/child-bounties/src/lib.rs (L713-765)
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

**File:** substrate/frame/bounties/src/lib.rs (L1058-1090)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```

**File:** prdoc/pr_11045.prdoc (L1-19)
```text
title: '[pallet-bounties]: add `reclaim_bounty_funds` to reclaim stranded funds from
  closed bounty accounts'
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/10996

    This PR adds a permissionless `reclaim_bounty_funds` extrinsic that moves all
    funds stranded in a closed bounty's account back to the treasury in a single
    call. It reclaims both the native token and any fungible assets configured via
    the `TransferAllAssets` associated type. Native funds are moved using
    `transfer_all` semantics (reducible balance with `Expendable` preservation) so
    locks and freezes are respected. The call is free on success and paid on a no-op,
    so it cannot be used to grief the network.
crates:
- name: pallet-bounties
  bump: major
- name: rococo-runtime
  bump: major
```
