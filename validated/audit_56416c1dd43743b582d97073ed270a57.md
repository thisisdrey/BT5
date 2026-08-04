### Title
Bridged local assets keep an untrusted `Freezer`/`Admin` role that can permanently freeze bridge-locked balances - ([File: bridges/snowbridge/runtime/runtime-common/src/v2/register_token.rs])

### Summary
The Solana report's root cause is that a lockbox accepts an SPL mint without checking whether that mint's `freeze_authority` is null, letting the (untrusted) mint authority freeze bridge-token accounts and permanently block withdrawals. The same class of bug exists in Snowbridge's `register_token` origin checks: `LocalAssetOwner` only verifies that the caller equals the `pallet_assets` **Owner** of the to-be-bridged asset, but never checks or clears the asset's separate **Freezer**/**Admin** roles before the asset is accepted into the bridging flow.

### Finding Description
`LocalAssetOwner::try_origin` in [1](#0-0)  only checks `who == AssetInspect::owner(asset_id)`. It performs no check of `Freezer`/`Admin` for the asset. This origin type is wired into `RegisterTokenOrigin` for the `snowbridge_pallet_system_frontend::Config` on Asset Hub, alongside `ForeignAssetOwner`, as seen in [2](#0-1) .

`pallet_assets::create` is a permissionless call (`### Permissionless Functions` doc list) that lets any signed account create an asset and become its Owner, Issuer, Admin **and** Freezer simultaneously, as documented in [3](#0-2) . `freeze`/`freeze_asset` require only that `origin == d.freezer` (no relationship to Owner is enforced, and Owner and Freezer can be, and by default are, the same account): [4](#0-3) [5](#0-4) 

When that asset owner then calls `SnowbridgeSystemFrontend::register_token` (`bridges/snowbridge/pallets/system-frontend/src/lib.rs`, lines 225-252), `RegisterTokenOrigin::ensure_origin` (i.e. `LocalAssetOwner`) succeeds solely because caller == owner, and the asset is registered as a bridgeable token on Ethereum (`RegisterForeignToken` command sent to BridgeHub). No verification is done that the asset's `Freezer`/`Admin` field is unset, burned, or bound to a non-freezable authority (unlike, e.g., `pallet-assets-freezer` reason-based holds which cannot be abused by an arbitrary single key).

Once registered, users transfer the asset cross-chain; value is locked into the Ethereum sovereign account on Asset Hub while representations circulate on Ethereum (mirrored by the test at [6](#0-5)  which explicitly "mint[s] the asset into the bridge sovereign account, to mimic locked funds"). Because the original asset creator retains the `Freezer` role forever, they can call `Assets::freeze` on the Ethereum sovereign/reserve account or on any individual bridging user's account, or `Assets::freeze_asset` to freeze the whole asset class, at any time after registration — exactly mirroring the Solana `freeze_authority` primitive: bridge-locked and user balances can be rendered permanently untransferable, blocking withdrawals/redemptions.

### Impact Explanation
This is a permanent user-fund/bridge-state lock: an ordinary, unprivileged user (the asset creator — not a governance actor, admin, validator, or relayer) can, entirely through public dispatchables (`pallet_assets::create`, `register_token`, `pallet_assets::freeze`/`freeze_asset`), block all Ethereum-bound settlement or return-transfers for that asset, freezing the sovereign/reserve account holding bridge-locked balances or freezing arbitrary victim accounts trying to redeem bridged tokens. This falls squarely in the "permanent user-fund or bridge-state lock" impact category from the gate.

### Likelihood Explanation
High feasibility: no special privilege is required. Any account can `create` an asset (default Admin/Freezer = creator), immediately be the sole `Owner` satisfying `LocalAssetOwner`, call `register_token` to onboard it into the bridge, wait for users/liquidity to accumulate against the bridge sovereign account, then call `freeze`/`freeze_asset` — a completely public, no-collusion attack path.

### Recommendation
Before accepting an asset for bridging via `LocalAssetOwner`/`ForeignAssetOwner`, require that the asset's `Freezer` and `Admin` roles be null/burned or bound to an un-freezable/immutable authority (e.g., verify via `pallet_assets::roles::Inspect` that freezer/admin equal a fixed sentinel, or require `set_team` to relinquish these roles before/at registration time), analogous to requiring `freeze_authority == None` for the Solana mint. Alternatively, disallow bridging of assets whose Freezer role is not permanently locked, or force freeze/thaw rights on bridged assets to route only through the bridge's own trusted authority.

### Proof of Concept
1. Attacker calls `pallet_assets::create(origin=Attacker, id=X, admin=Attacker, min_balance=1)` — Attacker becomes Owner/Admin/Issuer/Freezer of asset `X`.
2. Attacker mints/holds asset `X` and calls `SnowbridgeSystemFrontend::register_token(origin=Attacker, asset_id=X, ...)`. `LocalAssetOwner::try_origin` passes because `who == owner`.
3. Victims bridge asset `X` to Ethereum; local balances accumulate in the Ethereum sovereign account (as reproduced by the existing test pattern at lines 766-778 of `snowbridge_v2_inbound.rs`).
4. Attacker calls `pallet_assets::freeze(origin=Attacker, id=X, who=<ethereum_sovereign>)` or `freeze_asset(origin=Attacker, id=X)`.
5. All subsequent transfer/burn/redeem operations against asset `X` fail with `Error::Frozen`, permanently locking bridged user funds — no governance, admin, or malicious relayer involved.

### Citations

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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/bridge_to_ethereum_config.rs (L50-68)
```rust
	type RegisterTokenOrigin = EitherOf<
		EitherOf<
			LocalAssetOwner<
				AssetIdForTrustBackedAssetsConvert<TrustBackedAssetsPalletLocation, Location>,
				Assets,
				AccountId,
				AssetIdForTrustBackedAssets,
				Location,
			>,
			ForeignAssetOwner<
				(
					FromSiblingParachain<parachain_info::Pallet<Runtime>, Location>,
					xcm_config::bridging::to_rococo::RococoAssetFromAssetHubRococo,
				),
				ForeignAssets,
				AccountId,
				LocationToAccountId,
				Location,
			>,
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L766-778)
```rust
	let ethereum_sovereign: AccountId = snowbridge_sovereign();

	AssetHubWestend::fund_accounts(vec![(ethereum_sovereign.clone(), INITIAL_FUND)]);

	// Mint the asset into the bridge sovereign account, to mimic locked funds
	AssetHubWestend::mint_asset(
		<AssetHubWestend as Chain>::RuntimeOrigin::signed(AssetHubWestendAssetOwner::get()),
		RESERVABLE_ASSET_ID,
		ethereum_sovereign.clone(),
		TOKEN_AMOUNT,
	);

	let token_id = TokenIdOf::convert_location(&asset_id_after_reanchored).unwrap();
```
