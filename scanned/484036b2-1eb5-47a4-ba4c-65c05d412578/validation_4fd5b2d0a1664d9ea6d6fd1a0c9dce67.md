## Analysis

`pallet-child-bounties` is wired into the live Rococo relay-chain runtime (`polkadot/runtime/rococo/src/lib.rs`, weights present at `polkadot/runtime/rococo/src/weights/pallet_child_bounties.rs`), so it is in-scope.

Child-bounty accounts are deterministic, attacker-computable addresses (`PalletId::into_sub_account_truncating(("cb", parent_bounty_id, child_bounty_id))`), analogous to a "contract address" that anyone can send value to before or after its lifecycle ends. Unlike the parent `pallet-bounties`, which received a dedicated `reclaim_bounty_funds` extrinsic specifically to sweep stray/asset balances left in closed bounty accounts [1](#0-0) , `pallet-child-bounties` has no equivalent recovery mechanism for its own sub-accounts. [2](#0-1)  defines `child_bounty_account_id` as a deterministic sub-account. All value-moving paths in this pallet — `claim_child_bounty` [3](#0-2)  and `impl_close_child_bounty` (used by `close_child_bounty`) [4](#0-3)  — only ever move the pallet's native `Currency` (`T::Currency::transfer(&child_bounty_account, ..., AllowDeath)`), and only the balance amount known/expected by the pallet at the time of the call. There is no `TransferAllAssets`-style sweep, and no permissionless `reclaim_*` extrinsic analogous to `pallet_bounties::reclaim_bounty_funds`.

### Title
Child-bounty sub-accounts have no fallback/reclaim path for stray or residual funds - (File: substrate/frame/child-bounties/src/lib.rs)

### Summary
`pallet-child-bounties` computes a deterministic, publicly-known sub-account per child bounty (`child_bounty_account_id`). Both terminal state transitions (`claim_child_bounty`, `close_child_bounty`) transfer only the pallet-tracked native balance out of that account and never sweep non-native assets or residual dust that was not accounted for by the pallet's own bookkeeping. Once the `ChildBounties` storage entry is removed, the sub-account address is orphaned with no dispatchable capable of recovering anything left in it — mirroring the reported class of bug ("contract with no fallback to reject/redirect unexpected value transfers, causing funds to become permanently stuck").

### Finding Description
- `child_bounty_account_id(parent_bounty_id, child_bounty_id)` is a pure function of two public indices, so any account can pre-compute it and send funds (native or otherwise) to it at any time — before creation, during its active life, or after it has been claimed/closed. [2](#0-1) 
- `claim_child_bounty` only ever moves `T::Currency::free_balance(&child_bounty_account)` split into `curator_fee` and `payout`; if additional non-native assets (e.g., `pallet-assets` tokens) were sent to this account, they are never referenced or moved. [3](#0-2) 
- `impl_close_child_bounty` similarly moves only the native `free_balance` to the parent bounty account and then deletes the storage record — after which the account ID becomes unreachable from any pallet extrinsic. [5](#0-4) 
- The parent `pallet-bounties` was explicitly patched to add `reclaim_bounty_funds`, a permissionless call that sweeps both native and configured asset balances out of closed bounty accounts back to the treasury, precisely because "accidental refund" and asset-blocking scenarios left funds stranded. [6](#0-5)  No sibling fix exists for child-bounty accounts, even though they are structurally identical deterministic sub-accounts subject to the same "someone sends value to a known account" primitive.

### Impact Explanation
Funds (particularly non-native assets, or native dust sent after a child bounty is claimed/closed) sent to a child-bounty sub-account can become permanently locked with no on-chain path to recover them — a direct instance of "permanent user-fund lock," which is explicitly in the accepted impact set for this program. This does not require a malicious validator, governance actor, or leaked key; any ordinary account can trigger the stuck-fund condition by transferring to the well-known deterministic address.

### Likelihood Explanation
Likelihood is moderate: the trigger requires only a plain balance/asset transfer to a computable `AccountId`, which is trivial for any user (accidental or adversarial) to perform, and the closed/claimed states are reached in the normal child-bounty lifecycle. The absence of an equivalent to `reclaim_bounty_funds` for child bounties means there is no window in which the funds can later be recovered once the storage entry (`ChildBounties`) is removed.

### Recommendation
Add a permissionless `reclaim_child_bounty_funds`-style extrinsic (mirroring `pallet_bounties::reclaim_bounty_funds`) that, for a `(parent_bounty_id, child_bounty_id)` pair no longer present in `ChildBounties` storage, sweeps any residual native balance and configured assets from the deterministic child-bounty account back to the parent bounty or treasury account. Alternatively/additionally, extend `claim_child_bounty` and `close_child_bounty` to route through the same `TransferAllAssets` abstraction used in `pallet-bounties` so that stray assets sent during the bounty's active life are also captured at settlement time.

### Proof of Concept
1. Create and fund a parent bounty and add a child bounty via `add_child_bounty`; note the deterministic address `A = child_bounty_account_id(parent_bounty_id, child_bounty_id)`.
2. Before or after the child bounty reaches `PendingPayout`/is claimed, transfer an unrelated asset (via `pallet-assets::transfer`) directly to `A`.
3. Complete the child-bounty lifecycle normally (`award_child_bounty` → `claim_child_bounty`), which removes the `ChildBounties` storage entry for that index (`substrate/frame/child-bounties/src/lib.rs:759-763`).
4. Observe that the asset balance at `A` is unchanged and unreachable — no pallet extrinsic references `A` anymore, and the deterministic address can be recomputed by anyone but never drained.

Note: I was not able to fully verify whether a newer unified pallet (`substrate/frame/multi-asset-bounties`, seen referenced in test files) has since superseded `pallet-child-bounties` at the runtime-wiring level and closes this gap; the index shows `pallet-child-bounties` still compiled into the Rococo runtime with its own weights file, indicating it remains live, but I could not fully confirm the relationship between the two pallets due to index size limits. A Devin session with full repository access would be needed to confirm which pallet is actually deployed on production Polkadot-ecosystem chains.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L1047-1090)
```rust
		/// Reclaim funds stranded in a closed bounty's account back to the treasury.
		///
		/// Permissionless. Moves all remaining assets from a closed bounty's account back to the
		/// treasury in a single call. Which assets are swept depends on the `TransferAllAssets`
		/// configuration.
		///
		/// The call is free if funds were reclaimed and paid otherwise, so no-op calls cannot be
		/// used to grief the network. Emits `BountyFundsReclaimed` on success.
		///
		/// ## Complexity
		/// - O(A) where A is the number of relevant assets configured in `TransferAllAssets`.
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

**File:** substrate/frame/child-bounties/src/lib.rs (L713-744)
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
```

**File:** substrate/frame/child-bounties/src/lib.rs (L853-862)
```rust
	/// The account ID of a child-bounty account.
	pub fn child_bounty_account_id(
		parent_bounty_id: BountyIndex,
		child_bounty_id: BountyIndex,
	) -> T::AccountId {
		// This function is taken from the parent (bounties) pallet, but the
		// prefix is changed to have different AccountId when the index of
		// parent and child is same.
		T::PalletId::get().into_sub_account_truncating(("cb", parent_bounty_id, child_bounty_id))
	}
```

**File:** substrate/frame/child-bounties/src/lib.rs (L934-960)
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

				// Remove the child-bounty description.
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;

				Self::deposit_event(Event::<T>::Canceled {
					index: parent_bounty_id,
					child_index: child_bounty_id,
				});
				Ok(())
			},
		)
	}
```
