## Analog Found: Single-Step, Unconfirmed Ownership Transfer in `pallet-assets`

The external report's core invariant — "critical ownership handoff must not be completable by the current owner alone, since a typo'd/uncontrolled address permanently locks all `onlyOwner`-style functions" — has a direct, exploitable local analog in `pallet-assets::transfer_ownership`.

### Title
Unconfirmed single-step `transfer_ownership` in `pallet-assets` permanently locks owner-gated functions and reserved deposits on address error - (`substrate/frame/assets/src/lib.rs`)

### Summary
`pallet-assets` implements asset ownership transfer as a single unilateral step: the current owner calls `transfer_ownership` with a target `AccountIdLookupOf<T>`, and the pallet immediately repatriates the reserved deposit and overwrites `details.owner` with no confirmation from the new owner. [1](#0-0) 

By contrast, the sibling pallets `pallet-nfts` and `pallet-uniques` already require the *new* owner to first call `set_accept_ownership` before `transfer_ownership` can succeed, rejecting the call with `Error::Unaccepted` otherwise. [2](#0-1) [3](#0-2) 

### Finding Description
`Pallet::transfer_ownership` for an asset only checks that `origin == details.owner`, looks up the new owner via `T::Lookup::lookup(owner)`, and then unconditionally repatriates the combined asset+metadata deposit and writes `details.owner = owner` — there is no acceptance/claim step from the destination account: [4](#0-3) 

`details.owner` is the sole account authorized to call `set_team` (which assigns `issuer`, `admin`, `freezer`) and to call `transfer_ownership` itself again: [5](#0-4) 

If the caller supplies an address whose key is not controlled by anyone reachable (a typo, a burn address, an exchange deposit address, etc.), the transfer succeeds unconditionally: `T::Lookup::lookup` only validates that the value decodes to a valid `AccountId`, it does not (and cannot) verify the destination is reachable or intended. The pallet then moves the reserved deposit to that address via `repatriate_reserved` and finalizes `details.owner = owner` with no rollback path.

This directly mirrors the reported bug class: a one-step, unconfirmed ownership handoff where the current legitimate, non-privileged, non-governance caller (any asset creator/owner — an ordinary signed user, not root/governance) can — through their own human error — permanently lock every `owner`-gated function on that asset and permanently strand the reserved deposit.

### Impact Explanation
Once `details.owner` is set to an unreachable address:
- `set_team` can never be called again, so `issuer`/`admin`/`freezer` roles can never be rotated for that asset.
- `transfer_ownership` can never be called again (only the owner may invoke it), so the mis-transfer is irreversible.
- The asset's and metadata's reserved deposit (moved via `repatriate_reserved`) is permanently stranded in the unreachable account's reserved balance, unrecoverable by anyone.
- Since `admin`/`issuer`/`freezer` are separate from `owner` and remain functional independently, the asset itself may keep operating, but the governance surface of that asset (team rotation, future ownership correction) is permanently and unrecoverably locked — matching the report's "critical functions... will be locked" impact class, applied to a public, non-privileged asset-owner entrypoint rather than a governance actor.

### Likelihood Explanation
This requires no malicious peer, validator, collator, or privileged actor — only an ordinary signed asset owner making a single fat-fingered call to a public extrinsic, which is the exact "possible human error" scenario the original report described. `pallet-assets` is used in the Asset Hub / system parachain runtimes where end users routinely create and manage assets, making this a realistic and repeatable operator error, not a contrived edge case.

### Recommendation
Adopt the same two-step pattern already implemented in `pallet-nfts`/`pallet-uniques`: introduce a `set_accept_ownership`-equivalent storage item (e.g., pending-owner acceptance registry) and require the destination account to explicitly accept before `transfer_ownership` mutates `details.owner` and repatriates the deposit. This closes the same class of risk that `pallet-nfts`/`pallet-uniques` already mitigated, bringing `pallet-assets` to parity.

### Proof of Concept
1. Account `1` creates asset `0` via `Assets::create`, becoming `owner`, `admin`, `issuer`, `freezer`; deposit is reserved against account `1`.
2. Account `1` calls `Assets::transfer_ownership(origin=1, id=0, owner=<typo'd/uncontrolled AccountId>)`.
3. The call succeeds immediately: `repatriate_reserved` moves the reserved deposit to the uncontrolled address, and `details.owner` is set to it — no acceptance call exists in this pallet (contrast with the required `Nfts::set_accept_ownership` step, whose absence causes `Error::Unaccepted` in the sibling pallet's test at [6](#0-5) , but which `pallet-assets`'s own passing test at [7](#0-6)  shows succeeds with zero confirmation).
4. No account can subsequently call `Assets::transfer_ownership` or `Assets::set_team` for asset `0`, since `origin == details.owner` can never be satisfied again; the reserved deposit is permanently stranded.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L1322-1354)
```rust
		#[pallet::call_index(15)]
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

**File:** substrate/frame/assets/src/lib.rs (L1369-1394)
```rust
		pub fn set_team(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			issuer: AccountIdLookupOf<T>,
			admin: AccountIdLookupOf<T>,
			freezer: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let issuer = T::Lookup::lookup(issuer)?;
			let admin = T::Lookup::lookup(admin)?;
			let freezer = T::Lookup::lookup(freezer)?;
			let id: T::AssetId = id.into();

			Asset::<T, I>::try_mutate(id.clone(), |maybe_details| {
				let details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
				ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
				ensure!(origin == details.owner, Error::<T, I>::NoPermission);

				details.issuer = issuer.clone();
				details.admin = admin.clone();
				details.freezer = freezer.clone();

				Self::deposit_event(Event::TeamChanged { asset_id: id, issuer, admin, freezer });
				Ok(())
			})
		}
```

**File:** substrate/frame/nfts/src/features/transfer.rs (L124-141)
```rust
	pub(crate) fn do_transfer_ownership(
		origin: T::AccountId,
		collection: T::CollectionId,
		new_owner: T::AccountId,
	) -> DispatchResult {
		// Check if the new owner is acceptable based on the collection's acceptance settings.
		let acceptable_collection = OwnershipAcceptance::<T, I>::get(&new_owner);
		ensure!(acceptable_collection.as_ref() == Some(&collection), Error::<T, I>::Unaccepted);

		// Try to retrieve and mutate the collection details.
		Collection::<T, I>::try_mutate(collection, |maybe_details| {
			let details = maybe_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;
			// Check if the `origin` is the current owner of the collection.
			ensure!(origin == details.owner, Error::<T, I>::NoPermission);
			if details.owner == new_owner {
				return Ok(());
			}

```

**File:** substrate/frame/uniques/src/lib.rs (L868-884)
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

			Collection::<T, I>::try_mutate(collection.clone(), |maybe_details| {
				let details = maybe_details.as_mut().ok_or(Error::<T, I>::UnknownCollection)?;
				ensure!(origin == details.owner, Error::<T, I>::NoPermission);
				if details.owner == new_owner {
					return Ok(());
				}
```

**File:** substrate/frame/nfts/src/tests.rs (L618-621)
```rust
		assert_noop!(
			Nfts::transfer_ownership(RuntimeOrigin::signed(account(1)), 0, account(2)),
			Error::<Test>::Unaccepted
		);
```

**File:** substrate/frame/assets/src/tests.rs (L969-971)
```rust
		assert_ok!(Assets::transfer_ownership(RuntimeOrigin::signed(1), 0, 2));
		assert_eq!(Balances::reserved_balance(&2), 1);
		assert_eq!(Balances::reserved_balance(&1), 0);
```
