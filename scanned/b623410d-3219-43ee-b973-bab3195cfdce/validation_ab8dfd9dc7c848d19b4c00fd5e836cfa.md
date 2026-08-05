## Analog Found: `pallet-assets::transfer_ownership` lacks the two-step handoff that sibling pallets already implement

### Title
Single-step, unconfirmed asset ownership transfer permanently locks admin/issuer/freezer control if `owner` is unreachable or mistyped - (File: `substrate/frame/assets/src/lib.rs`)

### Summary
`pallet-assets::Pallet::transfer_ownership` moves full asset ownership (and its deposit) to any account supplied by the current owner, with no requirement that the new account has acknowledged or is even capable of acting as owner. This is the exact bug class from the external report: a `Swap.sol`-style `transferOwnership` that only checks the caller is the current owner and writes the new address unconditionally, without a two-step accept flow. `pallet-nfts` and `pallet-uniques` already fixed this class of bug for their own `transfer_ownership` calls by requiring the target to first call `set_accept_ownership`, but `pallet-assets` never received the equivalent hardening.

### Finding Description
`transfer_ownership` in `pallet-assets` only checks that `origin == details.owner` and that the asset is `Live`, then unconditionally rewrites `details.owner = owner`: [1](#0-0) 

There is no `OwnershipAcceptance` map, no `set_accept_ownership` call, and no zero/dead-account check comparable to what the recommended mitigation in the report calls for. Contrast this with the fixed pattern used by `pallet-nfts` and `pallet-uniques`, where `transfer_ownership` requires the prospective new owner to have pre-registered acceptance via `set_accept_ownership`/`do_set_accept_ownership`, and the call fails with `Error::Unaccepted` otherwise: [2](#0-1) [3](#0-2) 

`pallet-assets` has no such gate: the `Owner` role in `AssetDetails` is the master role that governs `set_team`, `set_metadata`, `freeze_asset`/`thaw_asset` permission grants, and further `transfer_ownership` calls themselves (since only the current owner can call it again). If the owner mistypes the target `AccountId`, sends to an unfunded/unused address, or the intended recipient never brings a signing key online, the asset's owner-gated functions become permanently inaccessible from the normal call surface — mirroring exactly the "breaking all functions with the `onlyOwner()` modifier" impact described in the source report.

### Impact Explanation
Once `owner` is set to an inaccessible account, the asset's issuer/admin/freezer roles can no longer be re-delegated by the community (only `Owner` can call `set_team`/`transfer_ownership` again), and the reserved deposit tied to the asset is stuck behind an owner that cannot act. Recovery is only possible through `force_asset_status`, a privileged `ForceOrigin` (root/governance) call — meaning this is not a self-healing accident, it results in a genuine, protocol-level fund/control lock requiring out-of-band governance intervention, consistent with the "permanent user-fund or bridge-state lock" impact class.

### Likelihood Explanation
This does not require a malicious actor, admin abuse, or any privileged action — it is triggered purely by the existing owner making an honest, unprivileged mistake in the same call flow that the original report describes (calling a public `transfer_ownership` extrinsic with a bad `owner` parameter). Given that asset IDs and account lookups are typed values entered by users/tooling, mistakes (wrong `AccountId`, copy-paste errors, wrong SS58 format resolving to a technically valid but uncontrolled account) are a realistic, recurring risk across any chain using `pallet-assets`.

### Recommendation
Bring `pallet-assets::transfer_ownership` in line with the pattern already implemented in `pallet-nfts`/`pallet-uniques`: add an `OwnershipAcceptance` (or equivalent) storage item and a `set_accept_ownership` extrinsic, and require the prospective owner to opt in before `transfer_ownership` can write `details.owner`. This closes the exact gap the external report flags without needing any new design — the mitigation pattern already exists in-repo and only needs to be applied consistently to `pallet-assets`.

### Proof of Concept
1. Create an asset via `pallet-assets::create`, becoming `Owner`.
2. Call `transfer_ownership(origin, id, owner = <mistyped/uncontrolled AccountId>)`.
3. Observe: [4](#0-3)  — the call succeeds unconditionally (no acceptance check), `details.owner` is now the uncontrolled account, and the deposit is repatriated to it.
4. Any subsequent `set_team`, `freeze_asset`/`thaw_asset` reconfiguration, or corrective `transfer_ownership` call now requires a signature from the inaccessible account and fails with `Error::<T, I>::NoPermission`, permanently locking the asset's admin functions absent a `force_asset_status` governance intervention.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1323-1354)
```rust
		pub fn transfer_ownership(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			owner: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let owner = T::Lookup::lookup(owner)?;
			let id: T::AssetId = id.into();

			Asset::<T, I>::try_mutate(id.clone(), |maybe_details| {
				let details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
				ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
				ensure!(origin == details.owner, Error::<T, I>::NoPermission);
				if details.owner == owner {
					return Ok(());
				}

				let metadata_deposit = Metadata::<T, I>::get(&id).deposit;
				let deposit = details.deposit + metadata_deposit;

				// `repatriate_reserved` is best-effort: reject any partial move so the recorded
				// deposit stays in sync with what is actually reserved on the owner.
				let remaining =
					T::Currency::repatriate_reserved(&details.owner, &owner, deposit, Reserved)?;
				ensure!(remaining.is_zero(), Error::<T, I>::IncompleteDepositTransfer);

				details.owner = owner.clone();

				Self::deposit_event(Event::OwnerChanged { asset_id: id, owner });
				Ok(())
			})
		}
```

**File:** substrate/frame/nfts/src/features/transfer.rs (L124-132)
```rust
	pub(crate) fn do_transfer_ownership(
		origin: T::AccountId,
		collection: T::CollectionId,
		new_owner: T::AccountId,
	) -> DispatchResult {
		// Check if the new owner is acceptable based on the collection's acceptance settings.
		let acceptable_collection = OwnershipAcceptance::<T, I>::get(&new_owner);
		ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted);

```

**File:** substrate/frame/uniques/src/lib.rs (L868-878)
```rust
		pub fn transfer_ownership(
			origin: OriginFor<T>,
			collection: T::CollectionId,
			new_owner: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let new_owner = T::Lookup::lookup(new_owner)?;

			let acceptable_collection = OwnershipAcceptance::<T, I>::get(&new_owner);
			ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted);

```
