### Title
`pallet-assets::transfer_ownership` transfers control of the asset class without revoking the seller's `admin`/`issuer`/`freezer` privileges - ([File: substrate/frame/assets/src/lib.rs])

### Summary
This is a direct structural analog of the Farcaster `IdRegistry` bug. In Farcaster, an `fid` has two co-equal authorities (`owner`, `recovery`), and `transfer()` only updates `owner` while leaving `recovery` (a lingering, full-power authority) in the hands of the seller. In `pallet-assets`, an asset class similarly has multiple co-equal authorities stored in `AssetDetails` — `owner`, `issuer`, `admin`, `freezer` — and `transfer_ownership` only updates the `owner` field, leaving `issuer`/`admin`/`freezer` unchanged and still pointing at the seller. [1](#0-0) 

### Finding Description
`AssetDetails` stores four independently-privileged roles for an asset class: `owner` (can change `owner`/`issuer`/`freezer`/`admin`), `issuer` (can mint), `admin` (can thaw, force-transfer, and burn from any account), and `freezer` (can freeze/block accounts). [1](#0-0) 

`transfer_ownership` is a public, signed extrinsic callable by the current `owner`. It mutates only the `owner` field of `AssetDetails` and repatriates the owner-held deposit; it never touches `issuer`, `admin`, or `freezer`: [2](#0-1) 

Changing the other three roles requires a *separate* call, `set_team`, which is gated by `origin == details.owner` — i.e. only the **new** owner (post-transfer) can call it, and only *after* they have taken over as owner: [3](#0-2) 

The module documentation itself confirms these are meant to be distinct, high-privilege roles callable by different accounts (`freeze`/`thaw` by admin, `mint` implicitly by issuer, `block` by freezer), and that `transfer_ownership` and `set_team` are two separate, independently-invoked operations: [4](#0-3) 

The consequence: unless the buyer of an asset class's ownership *also* independently negotiates and verifies that `set_team` has been executed to replace `issuer`/`admin`/`freezer`, the seller retains standing (not race-dependent) control to:
- mint unlimited new supply via the `issuer` role,
- force-transfer or burn any account's balance via the `admin` role,
- freeze/block any account (including the new owner) via the `freezer` role.

This is not a front-running scenario — it is a permanently-standing privilege retained by the previous owner until the new owner notices and calls `set_team`, exactly mirroring how Bob's `recovery` address in the Farcaster bug remains under his control indefinitely after `transfer()`, without needing to race anyone.

### Impact Explanation
An asset class "sale" via `transfer_ownership` conveys only the `owner` role, while the seller silently retains `issuer`, `admin`, and `freezer` — giving them the ability to mint unbacked supply, burn/force-transfer any holder's balance, or freeze the new owner's own account, all while the buyer reasonably believes they now fully control the asset. This breaks the "conserve value and settle exactly once to the rightful beneficiary" invariant for asset accounting: value (mint authority, freeze authority, burn authority) does not transfer atomically with the advertised ownership transfer.

### Likelihood Explanation
Any signed account that is the current `owner` of a live asset can call `transfer_ownership` today with no additional safeguard, and by default the four privileged roles frequently coincide with the same account at creation time. Any marketplace, migration script, or manual "sell my asset class" flow that relies on `transfer_ownership` alone (reasonably assuming ERC-20/721-style single-role transfer semantics) will leave the seller with residual, standing control. No malicious peer, validator, governance actor, or front-running race is required — only a signed `transfer_ownership` call followed by the buyer's (justified) belief that they now fully own the asset.

### Recommendation
Add a combined extrinsic (mirroring the Farcaster fix's `transferAndChangeRecovery`) that atomically transfers `owner` and resets `issuer`/`admin`/`freezer` to the new owner (or another specified set of accounts) in a single call — e.g. extend `transfer_ownership` to optionally reset the team, or clearly document/require that callers must always couple `transfer_ownership` with an atomic `set_team` call (ideally via a single dispatchable rather than two sequential ones, to avoid a window where the seller's `set_team` call could interleave). The existing `do_reset_team`/`ResetTeam` trait already provides the primitive to atomically set all four roles at once and should be exposed as the default path for ownership transfer. [5](#0-4) 

### Proof of Concept
1. Account `A` creates an asset via `Assets::create`, becoming `owner == issuer == admin == freezer == A`.
2. `A` calls `Assets::transfer_ownership(id, B)`. Now `owner == B`, but `issuer == admin == freezer == A` still (per `substrate/frame/assets/src/lib.rs:1349`, only `details.owner` is mutated).
3. `B` believes they have fully purchased the asset class and begins operating it (e.g. distributing tokens to holders).
4. `A`, still holding `admin`, calls `Assets::freeze_asset`/`force_transfer`/`burn` on any account, or, still holding `issuer`, calls `Assets::mint` to inflate supply — none of which require `B`'s consent, and none of which are blocked because the `NoPermission` checks in those calls only verify `origin == details.admin`/`details.issuer`, not `details.owner`.
5. `B` only regains full control once they discover the residual roles and call `Assets::set_team` (`substrate/frame/assets/src/lib.rs:1369-1394`) — by which time `A` may have already minted, frozen, or force-transferred funds. [6](#0-5) [7](#0-6)

### Citations

**File:** substrate/frame/assets/src/types.rs (L51-60)
```rust
#[derive(Clone, Encode, Decode, Eq, PartialEq, Debug, MaxEncodedLen, TypeInfo)]
pub struct AssetDetails<Balance, AccountId, DepositBalance> {
	/// Can change `owner`, `issuer`, `freezer` and `admin` accounts.
	pub owner: AccountId,
	/// Can mint tokens.
	pub issuer: AccountId,
	/// Can thaw tokens, force transfers and burn tokens from any account.
	pub admin: AccountId,
	/// Can freeze tokens.
	pub freezer: AccountId,
```

**File:** substrate/frame/assets/src/lib.rs (L113-124)
```rust
//! * `freeze`: Disallows further `transfer`s from an account; called by the asset class's Freezer.
//! * `thaw`: Allows further `transfer`s to and from an account; called by the asset class's Admin.
//! * `transfer_ownership`: Changes an asset class's Owner; called by the asset class's Owner.
//! * `set_team`: Changes an asset class's Admin, Freezer and Issuer; called by the asset class's
//!   Owner.
//! * `set_metadata`: Set the metadata of an asset class; called by the asset class's Owner.
//! * `clear_metadata`: Remove the metadata of an asset class; called by the asset class's Owner.
//! * `set_reserves`: Set the reserve information of an asset class; called by the asset class's
//!   Owner.
//! * `block`: Disallows further `transfer`s to and from an account; called by the asset class's
//!   Freezer.
//!
```

**File:** substrate/frame/assets/src/lib.rs (L1308-1354)
```rust
		/// Change the Owner of an asset.
		///
		/// Origin must be Signed and the sender should be the Owner of the asset `id`.
		///
		/// The asset (and metadata) deposit is moved from the current to the new owner. Fails
		/// with [`Error::IncompleteDepositTransfer`] if a lock or freeze on the current owner
		/// blocks the full transfer; clear it and retry.
		///
		/// - `id`: The identifier of the asset.
		/// - `owner`: The new Owner of this asset.
		///
		/// Emits `OwnerChanged`.
		///
		/// Weight: `O(1)`
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

**File:** substrate/frame/assets/src/lib.rs (L1368-1394)
```rust
		#[pallet::call_index(16)]
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

**File:** substrate/frame/assets/src/functions.rs (L1130-1144)
```rust
	pub(crate) fn do_reset_team(
		id: T::AssetId,
		owner: T::AccountId,
		admin: T::AccountId,
		issuer: T::AccountId,
		freezer: T::AccountId,
	) -> DispatchResult {
		let mut d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		d.owner = owner;
		d.admin = admin;
		d.issuer = issuer;
		d.freezer = freezer;
		Asset::<T, I>::insert(&id, d);
		Ok(())
	}
```
