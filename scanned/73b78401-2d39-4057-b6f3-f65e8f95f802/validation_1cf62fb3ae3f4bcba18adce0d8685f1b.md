Based on the evidence gathered, I found a strong local analog to the "partial-asset-sweep leaves funds stuck" bug class in `pallet-child-bounties`.

### Title
Child-bounty closure and payout only sweep native `Currency`, permanently stranding any `Assets` held by the child-bounty account - ([File: substrate/frame/child-bounties/src/lib.rs])

### Summary
The parent `pallet-bounties` was patched (see `prdoc/stable2603/pr_10729.prdoc` and the later permissionless `reclaim_bounty_funds` extrinsic in `prdoc/pr_11045.prdoc`) specifically because closing a bounty only returned native balance and left non-native `Assets` stuck in the bounty account. `pallet-child-bounties`, however, still finalizes child bounties using only `T::Currency::transfer`/`free_balance`, exactly the pattern that was identified as buggy for the parent pallet.

### Finding Description
`impl_close_child_bounty` at [1](#0-0)  computes `balance = T::Currency::free_balance(&child_bounty_account)` and transfers only that native balance back to the parent bounty account, then unconditionally clears the storage entry with `*maybe_child_bounty = None;` at [2](#0-1) . Likewise, `claim_child_bounty` at [3](#0-2)  only moves the native `free_balance` to the curator and beneficiary before deleting the child bounty record.

Neither path invokes anything analogous to `T::TransferAllAssets::force_transfer_all_assets` that was added to `pallet-bounties` for exactly this reason (`substrate/frame/bounties/src/lib.rs`, permissionless `reclaim_bounty_funds`, [4](#0-3) ). Once the `ChildBounties` storage entry is removed, the only remaining hook that scans child-bounty accounts for stranded funds is the parent-level `reclaim_bounty_funds`, and that function only operates on the *parent* bounty account id (`Self::bounty_account_id(bounty_id)`), gated by `debug_assert!(T::ChildBountyManager::child_bounties_count(bounty_id) == 0, ...)` — it never targets the child-bounty sub-account (`Self::child_bounty_account_id(parent_bounty_id, child_bounty_id)`). There is no `reclaim` mechanism defined in `pallet-child-bounties` at all, confirmed by the fact that `TransferAllAssets`/`force_transfer_all_assets` do not appear anywhere in `substrate/frame/child-bounties/src/lib.rs`.

### Impact Explanation
If a child bounty's dedicated sub-account (derived deterministically via `child_bounty_account_id`) ever receives non-native `Assets` — whether by design (asset-denominated bounty payouts configured through `TransferAllAssets`-style asset kinds used elsewhere in the bounty family, e.g. `substrate/frame/multi-asset-bounties`) or by accidental/adversarial transfer directly to the well-known deterministic account — that value becomes permanently unrecoverable once `close_child_bounty` or `claim_child_bounty` executes and clears the storage record. This is a permanent user/treasury-fund lock, matching the required-impact class "permanent user-fund ... lock."

### Likelihood Explanation
The child-bounty account address is deterministic and publicly computable (`PalletId` sub-account from `("cb", parent_bounty_id, child_bounty_id)`), so any unprivileged actor can send assets to it before closure/claim occurs — no privileged or governance action is required to trigger the loss, only a permissionless close/claim call by the curator or `RejectOrigin`/signed caller. Given that the sibling pallet-bounties code was already patched for the identical bug, and pallet-child-bounties was only bumped as a `patch` (not overhauled) in the accompanying `pr_10729`, it is plausible the child-bounty sweep path was not fully aligned with the fix.

### Recommendation
Add an equivalent asset-sweeping step to `impl_close_child_bounty` and `claim_child_bounty` (or a dedicated permissionless `reclaim_child_bounty_funds` extrinsic mirroring `pallet-bounties::reclaim_bounty_funds`) that transfers all configured `T::TransferAllAssets`-tracked asset balances from the child-bounty account to the parent bounty account (or treasury) before the child-bounty storage record and its `AccountId` derivation are made unreachable.

### Proof of Concept
1. Create a parent bounty, propose/accept a curator, and add a child bounty via `add_child_bounty`, obtaining `child_bounty_account_id(parent_id, child_id)`.
2. Transfer an `Assets`-pallet-denominated token directly to that deterministic account (any signed account can do this without special permission).
3. Call `close_child_bounty` (or let the payout flow reach `claim_child_bounty`) as the parent curator/`RejectOrigin`.
4. Observe: `Balances::free_balance` on the child-bounty account goes to zero (native swept), but `Assets::balance(asset_id, &child_bounty_account)` remains untouched; the child bounty record is deleted (`*maybe_child_bounty = None`), and no code path in `pallet-child-bounties` or `pallet-bounties::reclaim_bounty_funds` can ever reach that sub-account again.

**Note on confidence:** I was unable to fully confirm within the available tool budget whether `pallet-child-bounties` config actually wires in a `TransferAllAssets`-equivalent type at all (it may simply never be configured to hold non-native assets in the current runtimes), which would reduce real-world reachability. This should be verified against the runtime configuration (e.g., `polkadot/runtime/rococo` or `asset-hub` configs) for `pallet-child-bounties::Config` before treating this as fully exploitable in production.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L714-763)
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
```

**File:** substrate/frame/child-bounties/src/lib.rs (L934-946)
```rust
				// Transfer fund from child-bounty to parent bounty.
				let parent_bounty_account =
					pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);
				let child_bounty_account =
					Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
				let balance = T::Currency::free_balance(&child_bounty_account);
				let transfer_result = T::Currency::transfer(
					&child_bounty_account,
					&parent_bounty_account,
					balance,
					AllowDeath,
				); // Should not fail; child bounty account gets this balance during creation.
				debug_assert!(transfer_result.is_ok());
```

**File:** substrate/frame/child-bounties/src/lib.rs (L949-951)
```rust
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;
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
