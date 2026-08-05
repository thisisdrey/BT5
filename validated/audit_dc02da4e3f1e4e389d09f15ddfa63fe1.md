Audit Report

## Title
Permissionless-asset freezer can permanently lock unrelated liquidity in `pallet-asset-conversion` pools - (File: `substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`pallet_assets::create` lets any signed account mint an asset and become its `freezer`/`admin`/`issuer`. `pallet-asset-conversion`'s `do_create_pool`/`do_add_liquidity` accept arbitrary `T::AssetKind` pairs with no vetting of the counterpart asset's freezer, pooling other users' funds together with the attacker-controlled asset into one shared `pool_account`. The attacker can later freeze that account/asset, causing `do_remove_liquidity`/swap extrinsics to fail and revert atomically, permanently locking the honest side's funds too.

## Finding Description
`create` in [1](#0-0)  lets any signed origin (subject only to `T::CreateOrigin`, which is permissionless by default) become `issuer`/`admin`/`freezer` of a brand-new asset ID. `freeze` and `freeze_asset` then allow that same account to unilaterally set an account or the whole asset class to `AssetStatus::Frozen`/`AccountStatus::Frozen` with only a `d.freezer == origin` check — no counterparty consent required.

`do_create_pool` and `do_add_liquidity` in [2](#0-1)  never inspect who controls the freezer/admin role of either `AssetKind`; they simply compute a shared `pool_account` and transfer both assets into it via `T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)` and the equivalent for `asset2` at [3](#0-2) .

`do_remove_liquidity` burns the LP token first, then performs two sequential `T::Assets::transfer` calls moving each underlying asset out of `pool_account`: [4](#0-3) . `can_decrease`/`reducible_balance` in `pallet-assets` return `WithdrawConsequence::Frozen`/`Error::Frozen` whenever `details.status == AssetStatus::Frozen` or the account status is frozen: [5](#0-4)  and [6](#0-5) .

Because FRAME dispatchables are transactional — a `DispatchResult::Err` rolls back all storage writes for that extrinsic — if the second `T::Assets::transfer` (for the malicious/frozen asset) errors with `Token::Frozen`, the whole `do_remove_liquidity` call reverts, including the LP-token burn and the already-executed transfer of the first, non-malicious asset. There is no partial-withdrawal or force-thaw fallback anywhere in the pallet.

## Impact Explanation
Any liquidity pool paired with a permissionlessly-created asset is exposed: the asset's creator retains freezer rights indefinitely (unless explicitly reassigned) and can weaponize this at will by calling `freeze`/`freeze_asset` on the shared `pool_account`. Since `pallet-asset-conversion` bundles both legs of a pool into a single atomic dispatchable, this permanently locks all liquidity providers' funds in that pool — including the LP's share of the wholly unrelated, non-malicious asset (e.g., the native token side) — not merely the attacker's own asset. This matches the "permanent user-fund lock" category in the Polkadot SDK Impact Gate.

## Likelihood Explanation
The entire attack path uses only public, permissionless extrinsics available to any signed account: `pallet_assets::create` (no special origin required beyond default `CreateOrigin`), `pallet_asset_conversion::create_pool`/`add_liquidity`, and `pallet_assets::freeze`/`freeze_asset` (exercised using a role the attacker legitimately holds because they created the asset). No governance, validator, relayer, or malicious-peer assumption is needed, and the exploit is fully repeatable against any pool paired with an attacker-created asset.

## Recommendation
`pallet-asset-conversion` should either (a) restrict `AssetKind` in pool creation to assets whose freezer/admin role is governance-controlled or otherwise trusted (e.g., only `is_sufficient`/system-registered assets), or (b) make `do_remove_liquidity`/swap resilient to a frozen leg by allowing independent/best-effort withdrawal of the non-frozen asset instead of reverting the whole atomic operation, or by providing an escape hatch that lets LPs redeem the unaffected asset separately from the frozen one.

## Proof of Concept
1. Attacker calls `pallet_assets::create(origin, id=X, admin=attacker, min_balance=1)`, becoming issuer/admin/freezer of asset `X` ( [1](#0-0) ).
2. Attacker mints `X` to self and calls `pallet_asset_conversion::create_pool(origin, Native, X)`.
3. Victim calls `add_liquidity(origin, Native, X, amount1, amount2, ..., victim)`, depositing native tokens and `X` into `pool_account` per [3](#0-2) , receiving LP tokens.
4. Attacker calls `pallet_assets::freeze(origin, id=X, who=pool_account)` or `freeze_asset(origin, id=X)`.
5. Victim calls `remove_liquidity(...)`: the LP-token burn executes, the native-asset transfer may execute, but the `X` transfer fails with `Token::Frozen` per `can_decrease` ( [7](#0-6) ); the whole extrinsic reverts atomically, so the victim recovers none of their native-token liquidity while `X` remains frozen.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-892)
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

			let pool_info = PoolInfo { lp_token: lp_token.clone() };
			Pools::<T>::insert(pool_id.clone(), pool_info);

			Self::deposit_event(Event::PoolCreated {
				creator: creator.clone(),
				pool_id: pool_id.clone(),
				pool_account,
				lp_token,
			});

			if let Some(fee) = initial_fee {
				PoolFees::<T>::insert(&pool_id, fee);
				Self::deposit_event(Event::PoolFeeSet { pool_id: pool_id.clone(), fee });
			}

			Ok(pool_id)
		}

		/// Add liquidity to a pool.
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

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());

			let lp_token_amount: T::Balance;
			if total_supply.is_zero() {
				lp_token_amount = Self::calc_lp_amount_for_zero_supply(&amount1, &amount2)?;
				T::PoolAssets::mint_into(
					pool.lp_token.clone(),
					&pool_account,
					T::MintMinLiquidity::get(),
				)?;
			} else {
				let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
				let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
				lp_token_amount = side1.min(side2);
			}

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);

			T::PoolAssets::mint_into(pool.lp_token.clone(), mint_to, lp_token_amount)?;

			Self::deposit_event(Event::LiquidityAdded {
				who: who.clone(),
				mint_to: mint_to.clone(),
				pool_id,
				amount1_provided: amount1,
				amount2_provided: amount2,
				lp_token: pool.lp_token,
				lp_token_minted: lp_token_amount,
			});

			Ok(lp_token_amount)
		}
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

**File:** substrate/frame/assets/src/functions.rs (L252-256)
```rust
		let details = Asset::<T, I>::get(&id).ok_or(Error::<T, I>::Unknown)?;
		ensure!(details.status == AssetStatus::Live, Error::<T, I>::AssetNotLive);

		let account = Account::<T, I>::get(&id, who).ok_or(Error::<T, I>::NoAccount)?;
		ensure!(!account.status.is_frozen(), Error::<T, I>::Frozen);
```
