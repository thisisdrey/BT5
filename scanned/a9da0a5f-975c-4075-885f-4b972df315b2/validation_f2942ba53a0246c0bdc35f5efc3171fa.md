I have sufficient evidence to confirm the analog. Line 951-952 of `do_remove_liquidity` performs `T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)`, which internally calls `can_decrease`/`reducible_balance` in `substrate/frame/assets/src/functions.rs`, both of which check `account.status.is_frozen()` and return `Error::<T>::Frozen` before any transfer executes — this happens for both liquidity removal and swaps that draw the frozen asset out of `pool_account`.

### Title
Permissionless asset creator can freeze an AMM pool account in `pallet-asset-conversion`, permanently locking LP funds - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion::do_create_pool` accepts any `T::AssetKind` as a poolable asset without any restriction on that asset's freezer/admin role. Since `pallet-assets::create` is a public-origin call (`CreateOrigin: AsEnsureOriginWithArg<EnsureSigned<AccountId>>`) where the caller becomes `owner`/`issuer`/`admin`/`freezer` of the asset it creates, any unprivileged user can mint a token, pair it with a valuable asset (e.g. the native token) in an AMM pool, wait for liquidity providers to deposit real value, and then freeze the pool's own account for that asset — permanently locking both sides of the pool. This is the direct on-chain analog of the reported SPL "arbitrary input token freeze authority" bug: the pool blindly trusts the transferability of an externally-controlled token.

### Finding Description
`Pallet::create` in `substrate/frame/assets/src/lib.rs` (lines 843-889) lets any signed account create a new asset class where it becomes the `admin` and `freezer`: [1](#0-0) 

`pallet-asset-conversion::do_create_pool` (lines 729-788) allows any signed account to create a pool for `asset1`/`asset2` of arbitrary `T::AssetKind`, and calls `T::Assets::touch` to create an `Account` entry for the pool account for both assets — with no check on who controls the freezer/admin role of either asset: [2](#0-1) 

Once the pool account has a live `Account<T,I>` entry (a prerequisite satisfied automatically by pool creation/`add_liquidity`), the asset's `freezer` (the attacker who created the token) can call `pallet-assets::freeze` on the pool account: [3](#0-2) 

Any subsequent attempt to move that asset out of the pool account — via `remove_liquidity` or a swap — calls `T::Assets::transfer(asset, &pool_account, ..., amount, ...)`: [4](#0-3) 

which is rejected deterministically by `can_decrease`, since `account.status.is_frozen()` returns `true` for the pool account: [5](#0-4) 

Only the asset's `admin` can `thaw` the account (`substrate/frame/assets/src/lib.rs`, `thaw` call, origin must equal `d.admin`) — and the admin is the same attacker who created the token. There is no allowlist, no `mint::freeze_authority = None`-style validation, and no requirement that a poolable asset be free of an externally controlled freezer, unlike the recommendation given in the source report.

### Impact Explanation
Any liquidity provider who adds liquidity to a pool paired with an attacker-created asset can have their share of both assets (including the valuable side, e.g. native token or an established asset) permanently locked in the pool account once the attacker exercises the freezer role. This is a direct, permanent loss/lock of user funds, and it also stalls the pool for all users (DoS on that trading pair) — matching the "permanent user-fund lock" and "public underpriced work / stalls processing" impact classes accepted by the program. The attack requires no governance, validator, relayer, or privileged access — only the ability to permissionlessly create an asset and a pool, both public entry points.

### Likelihood Explanation
High. `pallet-assets::create` and `pallet-asset-conversion::create_pool`/`add_liquidity` are all permissionless, public-origin dispatchables reachable by any signed account on chains that configure `CreateOrigin` this way (e.g. the pattern seen in `TrustBackedAssetsInstance` on Asset Hub, where `CreateOrigin = AsEnsureOriginWithArg<EnsureSigned<AccountId>>`): [6](#0-5) 
No special conditions, timing, or race are required beyond luring at least one other LP into the malicious pool — a realistic scenario for any AMM that allows permissionless pool creation for arbitrary listed assets.

### Recommendation
- Require that assets accepted into `pallet-asset-conversion` pools are either restricted to a curated/allowlisted set (analogous to `ForceOrigin`-only asset creation) or that the freezer/admin role for the asset is renounced/set to a neutral, ungovernable account (e.g. burn address) before the asset participates in a pool.
- Alternatively, have `pallet-asset-conversion` implement its own `FrozenBalance`/freezer registration for pool accounts so pool accounts can never be frozen by third-party asset admins, or explicitly reject `do_create_pool`/`do_add_liquidity` when `T::Assets::freezer(asset) != None` and is not itself the asset conversion pallet.
- Surface a runtime-level warning or hard block when creating a pool where the counterpart asset's `admin`/`freezer` is not root/governance-controlled.

### Proof of Concept
1. Attacker calls `pallet_assets::create(id=X, admin=attacker, min_balance=1)` — attacker becomes `owner`/`issuer`/`admin`/`freezer` of asset `X` (`substrate/frame/assets/src/lib.rs:843-889`).
2. Attacker mints some `X` to self and calls `pallet_asset_conversion::create_pool(Native, X)`, then `add_liquidity(Native, X, ...)` to seed the pool minimally.
3. Victim LP calls `add_liquidity(Native, X, amount1_desired=<large native amount>, ...)`, transferring valuable native tokens into `pool_account` (`substrate/frame/asset-conversion/src/lib.rs:855-856`).
4. Attacker calls `pallet_assets::freeze(id=X, who=pool_account)` (`substrate/frame/assets/src/lib.rs:1192-1216`), which succeeds because `pool_account` already has a live `Account<T,I>` entry for `X` (created during `create_pool`/`add_liquidity` via `touch`).
5. Any subsequent `remove_liquidity` or swap involving asset `X` fails with `Error::<T>::Frozen` at the `T::Assets::transfer` call in `do_remove_liquidity`/swap functions, because `can_decrease` returns `Frozen` for the pool account (`substrate/frame/assets/src/functions.rs:199-205`).
6. Only the attacker (as `admin`) can `thaw` the account; the victim's native-token liquidity remains permanently locked in `pool_account` unless the attacker chooses to release it.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L863-879)
```rust
			Asset::<T, I>::insert(
				id.clone(),
				AssetDetails {
					owner: owner.clone(),
					issuer: admin.clone(),
					admin: admin.clone(),
					freezer: admin.clone(),
					supply: Zero::zero(),
					deposit,
					min_balance,
					is_sufficient: false,
					accounts: 0,
					sufficients: 0,
					approvals: 0,
					status: AssetStatus::Live,
				},
			);
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L726-770)
```rust
		/// Create a new liquidity pool.
		///
		/// **Warning**: The storage must be rolled back on error.
		pub(crate) fn do_create_pool(
			creator: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			initial_fee: Option<Permill>,
		) -> Result<T::PoolId, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);
			if let Some(fee) = initial_fee {
				ensure!(fee <= T::MaxSwapFee::get(), Error::<T>::FeeTooHigh);
			}

			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// pay the setup fee
			let fee =
				Self::withdraw(T::PoolSetupFeeAsset::get(), creator, T::PoolSetupFee::get(), true)?;
			T::PoolSetupFeeTarget::on_unbalanced(fee);

			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, creator)?
			};

			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, creator)?
			};

			let lp_token = NextPoolAssetId::<T>::get()
				.or(T::PoolAssetId::initial_value())
				.ok_or(Error::<T>::IncorrectPoolAssetId)?;
			let next_lp_token_id = lp_token.increment().ok_or(Error::<T>::IncorrectPoolAssetId)?;
			NextPoolAssetId::<T>::set(Some(next_lp_token_id));

			T::PoolAssets::create(lp_token.clone(), pool_account.clone(), false, 1u32.into())?;
			if T::PoolAssets::should_touch(lp_token.clone(), &pool_account) {
				T::PoolAssets::touch(lp_token.clone(), &pool_account, creator)?
			};
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L951-952)
```rust
			T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)?;
			T::Assets::transfer(asset2, &pool_account, withdraw_to, amount2, Expendable)?;
```

**File:** substrate/frame/assets/src/functions.rs (L199-205)
```rust
		let account = match Account::<T, I>::get(&id, who) {
			Some(a) => a,
			None => return BalanceLow,
		};
		if account.status.is_frozen() {
			return Frozen;
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L273-291)
```rust
type TrustBackedAssetsCall = pallet_assets::Call<Runtime, TrustBackedAssetsInstance>;
impl pallet_assets::Config<TrustBackedAssetsInstance> for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Balance = Balance;
	type AssetId = AssetIdForTrustBackedAssets;
	type AssetIdParameter = codec::Compact<AssetIdForTrustBackedAssets>;
	type ReserveData = ();
	type Currency = Balances;
	type CreateOrigin = AsEnsureOriginWithArg<EnsureSigned<AccountId>>;
	type ForceOrigin = AssetsForceOrigin;
	type AssetDeposit = AssetDeposit;
	type MetadataDepositBase = MetadataDepositBase;
	type MetadataDepositPerByte = MetadataDepositPerByte;
	type ApprovalDeposit = ApprovalDeposit;
	type StringLimit = AssetsStringLimit;
	type Holder = ();
	type Freezer = AssetsFreezer;
	type Extra = ();
	type WeightInfo = weights::pallet_assets_local::WeightInfo<Runtime>;
```
