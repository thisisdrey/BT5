Audit Report

## Title
Bridged local assets keep an untrusted `Freezer`/`Admin` role that can permanently freeze bridge-locked balances - ([File: bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs])

## Summary
`LocalAssetOwner::try_origin` only verifies that the caller equals the `pallet_assets` **Owner** of an asset before allowing it to be registered as a bridgeable token, but never checks or clears the asset's separate **Freezer**/**Admin** roles. Since `pallet_assets::create` is permissionless and by default grants the creator all four roles (Owner, Issuer, Admin, Freezer), any unprivileged user can create an asset, register it for bridging, wait for other users/liquidity to accumulate value against it (e.g., in the Ethereum sovereign account), and then call `freeze`/`freeze_asset` to permanently block transfers of that asset.

## Finding Description
`LocalAssetOwner::try_origin` in `bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs` checks only `who == AssetInspect::owner(asset_id)` [1](#0-0) . This is wired into `RegisterTokenOrigin` in the Asset Hub bridge-to-Ethereum config alongside `ForeignAssetOwner`, which similarly checks only ownership via `AssetInspect::owner` and never inspects Freezer/Admin roles [2](#0-1) .

`register_token` in the system-frontend pallet calls `T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)` and, on success, builds and sends a `RegisterForeignToken` transact call to BridgeHub with no further role verification [3](#0-2) . `pallet_assets::create` is documented as permissionless, and the resulting asset's Admin/Issuer/Freezer default to the creator, per the pallet's own documentation of privileged calls tied to the Freezer role (`freeze`, `freeze_asset`) [4](#0-3) . `freeze` and `freeze_asset` are gated only by `origin == d.freezer`, with no relationship to the asset Owner enforced [5](#0-4) [6](#0-5) .

I confirmed the exact code as cited exists in the repository and matches the claim's description precisely. The origin check genuinely omits any Freezer/Admin verification, and `pallet_assets` genuinely allows the same account to hold Owner and Freezer simultaneously with no cross-check between `register_token`'s origin gate and the Freezer role.

## Impact Explanation
If exploited, this allows an unprivileged asset creator to permanently freeze balances of a token that has been bridged (including the Ethereum sovereign/reserve account holding locked funds), blocking redemptions/withdrawals for that specific asset. This matches the "permanent user-fund or bridge-state lock" category in the impact gate, but the scope of harm is limited to funds denominated in assets the attacker itself created and chose to register — it cannot affect DOT, other pre-existing trusted assets, or the bridge's core proof/settlement logic (nonce, receipt, header binding, etc. remain unaffected). The lock is real but confined to assets an attacker deliberately set up as a trap, requiring victims to actually bridge that specific attacker-created asset.

## Likelihood Explanation
The attack path is fully public and requires no privilege beyond ordinary signed transactions: `pallet_assets::create` (permissionless), `SnowbridgeSystemFrontend::register_token` (any signed caller satisfying `LocalAssetOwner`), and `pallet_assets::freeze`/`freeze_asset` (gated only on self-granted Freezer role). No collusion, governance action, or compromised relayer/prover is needed. However, exploitability depends on convincing other users to bridge value into an attacker-created asset, which is a social/economic precondition rather than a purely technical one — a security-conscious user or front-end would need to interact with an asset with no established trust, reducing practical severity somewhat but not eliminating the underlying code-level gap.

## Recommendation
Before accepting an asset for bridging via `LocalAssetOwner`/`ForeignAssetOwner`, verify that the asset's Freezer and Admin roles are null, burned, or bound to an immutable/non-abusable authority (e.g., check via `pallet_assets::roles::Inspect` and require `set_team` to relinquish these roles prior to or as part of registration), or restrict which asset classes are eligible for bridging to those with pre-vetted/locked team roles.

## Proof of Concept
1. Attacker calls `pallet_assets::create(origin=Attacker, id=X, admin=Attacker, min_balance=1)`, becoming Owner/Admin/Issuer/Freezer of asset `X`.
2. Attacker calls `SnowbridgeSystemFrontend::register_token(origin=Attacker, asset_id=X, ...)`; `LocalAssetOwner::try_origin` succeeds because `who == owner`, with no Freezer check.
3. Victims bridge asset `X` to Ethereum, causing balances to accumulate against the Ethereum sovereign account on Asset Hub (mirrored by the pattern in `snowbridge_v2_inbound.rs` lines 766-778, which mints an asset into the bridge sovereign account to simulate locked funds).
4. Attacker calls `pallet_assets::freeze(origin=Attacker, id=X, who=<ethereum_sovereign>)` or `freeze_asset(origin=Attacker, id=X)`.
5. Subsequent transfers/burns/redemptions of asset `X` fail with `Error::Frozen`, permanently locking bridged funds for that asset with no governance or malicious relayer involved.

### Citations

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L36-53)
```rust
	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let origin_location = EnsureXcm::<Everything, L>::try_origin(origin.clone())?;
		if !IsForeign::contains(asset_location, &origin_location) {
			return Err(origin);
		}
		let asset_location: Location = asset_location.clone().into();
		let owner = AssetInspect::owner(asset_location.into()).ok_or_else(|| origin.clone())?;
		let location: Location = origin_location.clone().into();
		let from =
			LocationToAccountId::convert_location(&location).ok_or_else(|| origin.clone())?;
		if from != owner {
			return Err(origin);
		}
		Ok(location.into())
	}
```

**File:** bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs (L85-96)
```rust
	fn try_origin(
		origin: RuntimeOrigin,
		asset_location: &L,
	) -> Result<Self::Success, RuntimeOrigin> {
		let who = ensure_signed(origin.clone()).map_err(|_| origin.clone())?;
		let asset_id = MatchAssetId::convert(asset_location).ok_or_else(|| origin.clone())?;
		let owner = AssetInspect::owner(asset_id.into()).ok_or_else(|| origin.clone())?;
		if who != owner {
			return Err(origin);
		}
		Ok(who.into())
	}
```

**File:** bridges/snowbridge/pallets/system-frontend/src/lib.rs (L225-252)
```rust
		pub fn register_token(
			origin: OriginFor<T>,
			asset_id: Box<VersionedLocation>,
			metadata: AssetMetadata,
			fee_asset: Asset,
		) -> DispatchResult {
			ensure!(!Self::export_operating_mode().is_halted(), Error::<T>::Halted);

			let asset_location: Location =
				(*asset_id).try_into().map_err(|_| Error::<T>::UnsupportedLocationVersion)?;
			let origin_location = T::RegisterTokenOrigin::ensure_origin(origin, &asset_location)?;

			let ether_gained = if origin_location.is_here() {
				// Root origin/location does not pay any fees/tip.
				0
			} else {
				Self::swap_fee_asset_and_burn(origin_location.clone(), fee_asset)?
			};

			let call = Self::build_register_token_call(
				origin_location.clone(),
				asset_location,
				metadata,
				ether_gained,
			)?;

			Self::send_transact_call(origin_location, call)
		}
```

**File:** substrate/frame/assets/src/lib.rs (L85-126)
```rust
//! ### Permissionless Functions
//!
//! * `create`: Creates a new asset class, taking the required deposit.
//! * `transfer`: Transfer sender's assets to another account.
//! * `transfer_keep_alive`: Transfer sender's assets to another account, keeping the sender alive.
//! * `approve_transfer`: Create or increase an delegated transfer.
//! * `cancel_approval`: Rescind a previous approval.
//! * `transfer_approved`: Transfer third-party's assets to another account.
//! * `touch`: Create an asset account for non-provider assets. Caller must place a deposit.
//! * `refund`: Return the deposit (if any) of the caller's asset account or a consumer reference
//!   (if any) of the caller's account.
//! * `refund_other`: Return the deposit (if any) of a specified asset account.
//! * `touch_other`: Create an asset account for specified account. Caller must place a deposit.
//!
//! ### Permissioned Functions
//!
//! * `force_create`: Creates a new asset class without taking any deposit.
//! * `force_set_metadata`: Set the metadata of an asset class.
//! * `force_clear_metadata`: Remove the metadata of an asset class.
//! * `force_asset_status`: Alter an asset class's attributes.
//! * `force_cancel_approval`: Rescind a previous approval.
//!
//! ### Privileged Functions
//!
//! * `destroy`: Destroys an entire asset class; called by the asset class's Owner.
//! * `mint`: Increases the asset balance of an account; called by the asset class's Issuer.
//! * `burn`: Decreases the asset balance of an account; called by the asset class's Admin.
//! * `force_transfer`: Transfers between arbitrary accounts; called by the asset class's Admin.
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
//! Please refer to the [`Call`] enum and its associated variants for documentation on each
//! function.
```

**File:** substrate/frame/assets/src/lib.rs (L1192-1216)
```rust
		#[pallet::call_index(11)]
		pub fn freeze(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			who: AccountIdLookupOf<T>,
		) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();

			let d = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
			ensure!(
				d.status == AssetStatus::Live || d.status == AssetStatus::Frozen,
				Error::<T, I>::IncorrectStatus
			);
			ensure!(origin == d.freezer, Error::<T, I>::NoPermission);
			let who = T::Lookup::lookup(who)?;

			Account::<T, I>::try_mutate(&id, &who, |maybe_account| -> DispatchResult {
				maybe_account.as_mut().ok_or(Error::<T, I>::NoAccount)?.status =
					AccountStatus::Frozen;
				Ok(())
			})?;

			Self::deposit_event(Event::<T, I>::Frozen { asset_id: id, who });
			Ok(())
```

**File:** substrate/frame/assets/src/lib.rs (L1265-1280)
```rust
		#[pallet::call_index(13)]
		pub fn freeze_asset(origin: OriginFor<T>, id: T::AssetIdParameter) -> DispatchResult {
			let origin = ensure_signed(origin)?;
			let id: T::AssetId = id.into();

			Asset::<T, I>::try_mutate(id.clone(), |maybe_details| {
				let d = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
				ensure!(d.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);
				ensure!(origin == d.freezer, Error::<T, I>::NoPermission);

				d.status = AssetStatus::Frozen;

				Self::deposit_event(Event::<T, I>::AssetFrozen { asset_id: id });
				Ok(())
			})
		}
```
