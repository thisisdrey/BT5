Audit Report

## Title
Permissionless-asset freezer can permanently lock unrelated liquidity in `pallet-asset-conversion` pools - (File: `substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`pallet_assets::create` lets any signed account mint an asset and become its `issuer`/`admin`/`freezer` [1](#0-0) . `pallet-asset-conversion::do_create_pool`/`do_add_liquidity` accept arbitrary `T::AssetKind` pairs with no vetting of the counterpart asset's admin/freezer, pooling other users' funds together with the attacker-controlled asset into one `pool_account` [2](#0-1) . The attacker can later freeze that account/asset for their leg, causing `do_remove_liquidity`'s two sequential `T::Assets::transfer` calls to revert atomically, permanently locking the honest side's funds too.

## Finding Description
`create` sets `issuer = admin = freezer = admin` for a fully permissionlessly created asset (any signed account passes `T::CreateOrigin` in the standard configuration) [3](#0-2) . The freezer role can subsequently call `freeze` (per-account) or `freeze_asset` (whole class) unilaterally, gated only by `origin == d.freezer` — a role the attacker set to themselves at creation time [4](#0-3) [5](#0-4) .

`do_create_pool`/`do_add_liquidity` in `pallet-asset-conversion` never check who the freezer/admin of either `AssetKind` is; they compute a shared `pool_account` and transfer both assets into it [6](#0-5) [7](#0-6) .

`do_remove_liquidity` burns the LP token first and only then performs two sequential `T::Assets::transfer` calls moving each underlying asset out of `pool_account` [8](#0-7) . `can_decrease` (used by the transfer/withdraw consequence checks) returns `Frozen` if the asset class status is `Frozen` or if the specific account's status is frozen [9](#0-8) ; `reducible_balance` enforces the same via `ensure!(!account.status.is_frozen(), ...)` [10](#0-9) .

Because FRAME dispatch is transactional, a `Frozen` failure on the malicious asset's leg reverts the whole `remove_liquidity`/swap extrinsic, including the already-executed LP-token burn and the leg for the innocent asset. No code in `do_create_pool`/`do_add_liquidity` checks the freezer/admin trustworthiness of either `AssetKind`, and there is no partial-withdrawal or force-thaw escape hatch in `pallet-asset-conversion`.

## Impact Explanation
Any pool paired with a permissionlessly-created asset is exposed: its creator retains freezer rights (unless explicitly reassigned via `set_team`/`transfer_ownership`) and can weaponize this at any time by calling `freeze`/`freeze_asset` against the pool account. Since both legs of a pool are bundled into single atomic dispatchables (`do_add_liquidity`, `do_remove_liquidity`, and the swap functions), freezing one leg locks all liquidity providers' funds in the pool, including their share of the wholly unrelated, non-malicious asset — not merely the attacker's own asset. This matches the "permanent user-fund lock" impact category in the accepted impact gate.

## Likelihood Explanation
The attack requires no privileged access beyond what is freely available to any signed account: `pallet_assets::create`, `freeze`/`freeze_asset`, and `pallet_asset_conversion::create_pool`/`add_liquidity` are all standard, unprivileged, publicly callable extrinsics [11](#0-10) [12](#0-11) . No governance, validator, or malicious-peer assumption is needed; the root cause is a self-granted role from a public, permissionless entry point being later abused against a shared account that other users are induced to deposit into, not an externally trusted governance/admin acting maliciously. The attack is fully repeatable against any new pool paired with an attacker-created asset.

## Recommendation
`pallet-asset-conversion` should either (a) restrict permissible `AssetKind`s for pool creation to a vetted/system-registered set (e.g., via a configurable filter analogous to how Asset Hub gates `ForeignAssets::CreateOrigin`), or (b) make `remove_liquidity`/swap resilient to a frozen leg — e.g., support partial/best-effort withdrawal of the non-frozen asset independent of the frozen one, or provide a governance/force-thaw escape hatch scoped to `pool_account` so LPs are not permanently blocked from redeeming the non-malicious asset.

## Proof of Concept
1. Attacker calls `pallet_assets::create(origin, id=X, admin=attacker, min_balance=1)` — attacker becomes `issuer`/`admin`/`freezer` of `X` [13](#0-12) .
2. Attacker mints `X` to self and calls `pallet_asset_conversion::create_pool(origin, Native, X)`, which invokes `do_create_pool` and computes a shared `pool_account` [14](#0-13) .
3. Victim calls `add_liquidity(origin, Native, X, amount1, amount2, ..., victim)`, depositing native tokens and `X` into `pool_account` via `do_add_liquidity`, receiving LP tokens [7](#0-6) .
4. Attacker calls `pallet_assets::freeze(origin, id=X, who=pool_account)` or `freeze_asset(origin, id=X)` [4](#0-3) .
5. Victim calls `remove_liquidity(...)`: `do_remove_liquidity` burns the LP token, then the second `T::Assets::transfer` for `X` fails with `Token::Frozen` per `can_decrease` [8](#0-7) [15](#0-14) ; the whole extrinsic reverts atomically, including the LP-token burn, so the victim cannot redeem any of their native-token liquidity while `X` stays frozen.

### Citations

**File:** substrate/frame/assets/src/lib.rs (L843-889)
```rust
		pub fn create(
			origin: OriginFor<T>,
			id: T::AssetIdParameter,
			admin: AccountIdLookupOf<T>,
			min_balance: T::Balance,
		) -> DispatchResult {
			let id: T::AssetId = id.into();
			let owner = T::CreateOrigin::ensure_origin(origin, &id)?;
			let admin = T::Lookup::lookup(admin)?;

			ensure!(!Asset::<T, I>::contains_key(&id), Error::<T, I>::InUse);
			ensure!(!min_balance.is_zero(), Error::<T, I>::MinBalanceZero);

			if let Some(next_id) = T::AssetIdAllocator::next() {
				ensure!(id == next_id, Error::<T, I>::BadAssetId);
			}

			let deposit = T::AssetDeposit::get();
			T::Currency::reserve(&owner, deposit)?;

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
			ensure!(T::CallbackHandle::created(&id, &owner).is_ok(), Error::<T, I>::CallbackFailed);
			T::AssetIdAllocator::advance().map_err(|_| Error::<T, I>::AssetIdAllocationFailed)?;
			Self::deposit_event(Event::Created {
				asset_id: id,
				creator: owner.clone(),
				owner: admin,
			});

			Ok(())
		}
```

**File:** substrate/frame/assets/src/lib.rs (L1193-1217)
```rust
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

**File:** substrate/frame/assets/src/lib.rs (L1266-1280)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-759)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L791-856)
```rust
		pub(crate) fn do_add_liquidity(
			who: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount1_desired: T::Balance,
			amount2_desired: T::Balance,
			amount1_min: T::Balance,
			amount2_min: T::Balance,
			mint_to: &T::AccountId,
		) -> Result<T::Balance, DispatchError> {
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			ensure!(
				amount1_desired > Zero::zero() && amount2_desired > Zero::zero(),
				Error::<T>::WrongDesiredAmount
			);

			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;

				if amount2_optimal <= amount2_desired {
					ensure!(
						amount2_optimal >= amount2_min,
						Error::<T>::AssetTwoDepositDidNotMeetMinimum
					);
					amount1 = amount1_desired;
					amount2 = amount2_optimal;
				} else {
					let amount1_optimal = Self::quote(&amount2_desired, &reserve2, &reserve1)?;
					ensure!(
						amount1_optimal <= amount1_desired,
						Error::<T>::OptimalAmountLessThanDesired
					);
					ensure!(
						amount1_optimal >= amount1_min,
						Error::<T>::AssetOneDepositDidNotMeetMinimum
					);
					amount1 = amount1_optimal;
					amount2 = amount2_desired;
				}
			}

			ensure!(
				amount1.saturating_add(reserve1) >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::AmountOneLessThanMinimal
			);
			ensure!(
				amount2.saturating_add(reserve2) >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::AmountTwoLessThanMinimal
			);

			T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
			T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L941-952)
```rust
			// burn the provided lp token amount that includes the fee
			T::PoolAssets::burn_from(
				pool.lp_token.clone(),
				who,
				lp_token_burn,
				Expendable,
				Exact,
				Polite,
			)?;

			T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)?;
			T::Assets::transfer(asset2, &pool_account, withdraw_to, amount2, Expendable)?;
```

**File:** substrate/frame/assets/src/functions.rs (L187-205)
```rust
		if details.supply.checked_sub(&amount).is_none() {
			return Underflow;
		}
		if details.status == AssetStatus::Frozen {
			return Frozen;
		}
		if details.status == AssetStatus::Destroying {
			return UnknownAsset;
		}
		if amount.is_zero() {
			return Success;
		}
		let account = match Account::<T, I>::get(&id, who) {
			Some(a) => a,
			None => return BalanceLow,
		};
		if account.status.is_frozen() {
			return Frozen;
		}
```

**File:** substrate/frame/assets/src/functions.rs (L247-256)
```rust
	pub(super) fn reducible_balance(
		id: T::AssetId,
		who: &T::AccountId,
		keep_alive: bool,
	) -> Result<T::Balance, DispatchError> {
		let details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

		let account = Account::<T, I>::get(&id, who).ok_or(Error::<T, I>::NoAccount)?;
		ensure!(!account.status.is_frozen(), Error::<T, I>::Frozen);
```
