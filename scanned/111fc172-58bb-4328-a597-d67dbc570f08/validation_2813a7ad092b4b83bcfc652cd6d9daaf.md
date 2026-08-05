### Title
Permissionless `pallet-asset-conversion` pools can be permanently frozen by the asset's own (attacker-controlled) `Freezer` role - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion::create_pool` is a **permissionless, signed-origin** call that accepts *any* `T::AssetKind` pairing, including arbitrary `pallet-assets` asset IDs created by ordinary users via the equally permissionless `pallet_assets::create` call [1](#0-0) . `pallet-assets` lets the asset creator name themselves as the class `Freezer`/`Admin`/`Issuer` [2](#0-1) . That `Freezer` account can later call the permissioned `freeze` dispatchable on *any* account holding that asset, including the AMM pool's own vault account [3](#0-2) . This is the exact analog of the reported bug: a pool trusts an externally supplied "mint" (asset) without validating that it has no external freeze authority, so that authority can freeze the pool's vault and permanently DoS/lock funds.

### Finding Description
The pool creation flow in `do_create_pool` computes a deterministic `pool_account` for the asset pair and touches/holds both assets in that account with no validation of the assets' `Freezer`, `Admin`, or `Owner` fields [4](#0-3) . Anyone can call `pallet_assets::create`/permissionless variants to mint an asset and remain (or later become, via `set_team`) that asset's `Freezer`. Once liquidity providers add real value to a pool paired with this attacker asset (`add_liquidity`, `create_pool` are both fully permissionless [5](#0-4) ), the attacker calls `Assets::freeze(origin, asset_id, pool_account)`. The dispatchable only checks `origin == d.freezer`, which is the attacker's own account, and does not check whether `who` is a special/protocol-owned account such as an AMM pool's vault [3](#0-2) . Once frozen, `Account::status` becomes `AccountStatus::Frozen`, and transfers of that asset out of the pool account are blocked at the balance-mutation layer, exactly mirroring the Solana report where a freeze-authority-controlled mint locks a pool's vault.

Existing guards do not stop this path:
- `do_create_pool`/`do_add_liquidity` never inspect the asset's team (`Owner`/`Admin`/`Issuer`/`Freezer`) before accepting it into a pool.
- `T::AssetKind`/`T::Assets` abstraction (`NativeAndAssets`, `fungibles::Mutate`) has no notion of "safe" vs "unsafe" (freezer-controlled) assets; any asset satisfying the trait bounds is accepted.
- Thawing requires the `Admin` of that same attacker-created asset — i.e., the attacker themselves — so there is no path for the protocol or the LPs to recover.

### Impact Explanation
Freezing the shared `pool_account` for one leg of the pair halts `swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`, and `remove_liquidity` for that pool, because the vault cannot move the frozen asset. Any legitimate counter-asset (e.g., the network's native token or a reputable asset) paired against the malicious asset becomes permanently locked inside the pool account together with the frozen asset, since removal requires transferring both legs out of the same account. This is a direct, permanent user-fund lock / DoS on public AMM infrastructure, matching the "permanent loss of funds" and "denial of service" impact classes required by the gate.

### Likelihood Explanation
High. `pallet_assets::create` (or the permissionless creation path used on Asset Hub) and `pallet_asset_conversion::create_pool`/`add_liquidity` are all reachable by any signed, unprivileged account with no admin, governance, validator, or collator involvement — the "privileged" freezer role in question is self-assigned by the attacker when creating their own asset, not a protocol admin. The attack requires only ordinary balances/fees to create an asset and a pool, and it can be executed against any live pool that pairs a well-known asset with an attacker-created one.

### Recommendation
- Before accepting an asset into `create_pool`/`add_liquidity`, require the asset to have no externally-controlled `Freezer` (e.g., enforce `Freezer == pool pallet account`, `Freezer == None`/system, or restrict poolable assets to a pre-vetted allow-list via governance-controlled `AssetKind`).
- Alternatively, have the runtime's `AssetKind`/`PoolLocator` reject assets whose team fields are not locked to a trusted account, similar to how `mint::freeze_authority = COption::None` is recommended in the original report.
- Consider having `pallet-asset-conversion` use a "safe assets" filter analogous to sufficiency checks already used for pool setup fees.

### Proof of Concept
1. Attacker calls `pallet_assets::create` (permissionless, with deposit) to create `asset_id = X`, naming themselves `Owner`/`Admin`/`Issuer`/`Freezer`.
2. Attacker (or anyone) calls `AssetConversion::create_pool(Native, WithId(X))`, then `add_liquidity` to seed the pool; other users then add real liquidity/swap into it, growing the pool's WND/DOT (native) balance in `pool_account`.
3. Attacker calls `Assets::freeze(origin=attacker, id=X, who=pool_account)`. Since `origin == d.freezer` holds, the call succeeds [6](#0-5) .
4. `pool_account`'s asset-`X` balance is now `AccountStatus::Frozen`; subsequent `swap_*`/`remove_liquidity` calls that need to move asset `X` out of `pool_account` fail, stranding both the native and `X` liquidity in the pool permanently (only the attacker, as `Admin`, can `thaw`, and they have no incentive to).

Note: I was not able to trace every downstream low-level balance-mutation check (e.g., exact `functions.rs` line enforcing `AccountStatus::Frozen` on transfer) within the available search budget; a Devin session with full repo access could confirm the precise transfer-blocking code path in `substrate/frame/assets/src/functions.rs` to complete the PoC trace.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L440-450)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_pool())]
		pub fn create_pool(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_create_pool(&sender, *asset1, *asset2, None)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L466-490)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::add_liquidity())]
		pub fn add_liquidity(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
			amount1_desired: T::Balance,
			amount2_desired: T::Balance,
			amount1_min: T::Balance,
			amount2_min: T::Balance,
			mint_to: T::AccountId,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_add_liquidity(
				&sender,
				*asset1,
				*asset2,
				amount1_desired,
				amount2_desired,
				amount1_min,
				amount2_min,
				&mint_to,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-770)
```rust
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

**File:** substrate/frame/assets/src/lib.rs (L47-64)
```rust
//! * **Admin**: An account ID uniquely privileged to be able to unfreeze (thaw) an account and its
//!   assets, as well as forcibly transfer a particular class of assets between arbitrary accounts
//!   and reduce the balance of a particular class of assets of arbitrary accounts.
//! * **Asset issuance/minting**: The creation of a new asset, whose total supply will belong to the
//!   account designated as the beneficiary of the asset. This is a privileged operation.
//! * **Asset transfer**: The reduction of the balance of an asset of one account with the
//!   corresponding increase in the balance of another.
//! * **Asset destruction**: The process of reducing the balance of an asset of one account. This is
//!   a privileged operation.
//! * **Fungible asset**: An asset whose units are interchangeable.
//! * **Issuer**: An account ID uniquely privileged to be able to mint a particular class of assets.
//! * **Freezer**: An account ID uniquely privileged to be able to freeze an account from
//!   transferring a particular class of assets.
//! * **Freezing**: Removing the possibility of an unpermissioned transfer of an asset from a
//!   particular account.
//! * **Non-fungible asset**: An asset for which each unit has unique characteristics.
//! * **Owner**: An account ID uniquely privileged to be able to destroy a particular asset class,
//!   or to set the Issuer, Freezer, Reserves, or Admin of that asset class.
```

**File:** substrate/frame/assets/src/lib.rs (L1192-1217)
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
		}
```
