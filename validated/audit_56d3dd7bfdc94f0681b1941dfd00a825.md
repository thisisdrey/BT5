## Analysis

The core broken invariant in the Lavarage report is: **an asset issuer can unilaterally freeze a shared/pooled account holding a token used as collateral by other unrelated parties, and the protocol never validates or hedges against this before pooling funds into a shared account.** The direct analog in `polkadot-sdk` is `pallet-assets` combined with `pallet-asset-conversion`.

`pallet_assets::create` is a fully permissionless dispatchable — any signed account can create an asset class and becomes its `issuer`/`admin`/`freezer`: [1](#0-0) 

That freezer can later call `freeze` (per-account) or `freeze_asset` (whole class) at any time, unilaterally: [2](#0-1) [3](#0-2) 

`pallet-asset-conversion::create_pool`/`add_liquidity` are also permissionless and accept **any** `T::AssetKind` pair without validating who controls the asset's admin/freezer role, moving liquidity providers' funds of *both* assets into one shared `pool_account`: [4](#0-3) 

Crucially, `do_remove_liquidity` burns the LP's `lp_token` first and only afterward performs the two `T::Assets::transfer` calls that move each underlying asset out of `pool_account`: [5](#0-4) 

Because FRAME dispatch is transactional (a failing extrinsic rolls back all storage writes), if the malicious asset's transfer out of `pool_account` fails with `Frozen`, the entire extrinsic reverts — including the withdrawal of the *other*, non-malicious asset (e.g. the native DOT/WND side of the pool). The `Frozen` failure path is enforced in `can_decrease`/`reducible_balance`: [6](#0-5) 

So any user can: (1) permissionlessly create asset `X` via `pallet_assets::create`, becoming its `freezer`; (2) pair `X` with the native token (or any valuable asset) in a `pallet-asset-conversion` pool via `create_pool`/`add_liquidity`, drawing in other users' liquidity into the shared `pool_account`; (3) at will, call `freeze`/`freeze_asset` on `X` targeting the `pool_account`. From that point, `remove_liquidity` and `swap_*` for that pool permanently fail (transaction reverts atomically), locking **all** LPs' funds in the pool — not just the malicious asset's share, because the underlying-asset transfers are bundled in one atomic dispatchable. There is no code path in `pallet-asset-conversion` that checks an asset's freezer/admin trustworthiness before pooling, nor any mechanism to force-thaw or route around a frozen counterparty asset.

### Title
Permissionless-asset freezer can permanently lock unrelated liquidity in `pallet-asset-conversion` pools - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-assets::create` lets any signed account mint an asset and become its `freezer`. `pallet-asset-conversion::create_pool`/`add_liquidity` accept arbitrary `AssetKind` pairs with no vetting of the counterpart asset's admin/freezer, pooling other users' funds together with the attacker-controlled asset into one `pool_account`. The attacker can later freeze that account for their asset, causing `remove_liquidity`/swap extrinsics to revert atomically and permanently locking the honest side's funds too.

### Finding Description
- `create` in `substrate/frame/assets/src/lib.rs` (L843-889) sets `issuer = admin = freezer = admin` for a permissionlessly created asset.
- `freeze`/`freeze_asset` (L1192-1280) let that freezer lock a specific account or the whole asset class at will, with no counterparty consent.
- `do_create_pool`/`do_add_liquidity` in `substrate/frame/asset-conversion/src/lib.rs` (L729-892) never check who the freezer/admin of either `AssetKind` is; they simply transfer both assets into a computed `pool_account` (L855-856).
- `do_remove_liquidity` (L895-966) burns LP tokens first, then performs two sequential `T::Assets::transfer` calls out of `pool_account` (L951-952). `can_decrease`/`reducible_balance` in `substrate/frame/assets/src/functions.rs` (L187-205, L256) return `Frozen`/error if the account/asset is frozen.
- Because dispatchables execute transactionally, a `Frozen` failure on the malicious asset's leg reverts the whole `remove_liquidity` call, including the leg for the innocent asset (e.g., native token).

### Impact Explanation
Any pool paired with a permissionlessly-created asset is exposed: its creator retains freezer rights forever (unless explicitly reassigned via `set_team`) and can weaponize this to lock the `pool_account` at any time. Since `pallet-asset-conversion` bundles both legs of the pool into single atomic operations, this locks *all* liquidity providers' funds in the pool — including their share of the wholly unrelated, non-malicious asset — not merely the attacker's own asset. This matches the "permanent user-fund lock" impact category.

### Likelihood Explanation
Requires no privileged access beyond what's freely available to any signed account: creating an asset and pairing it in a pool are both public, permissionless entry points (`pallet_assets::create`, `pallet_asset_conversion::create_pool`/`add_liquidity`). No governance, validator, or malicious-peer assumption is needed — the attacker acts entirely within the bounds of normal, unprivileged transaction flow.

### Recommendation
`pallet-asset-conversion` should either (a) disallow pairing with assets whose freezer/admin is not a trusted/governance-controlled account (e.g., restrict `AssetKind` to `is_sufficient`/system-registered assets, similar to how Asset Hub gates `ForeignAssets::CreateOrigin`), or (b) make `remove_liquidity`/swap resilient to a frozen leg — e.g., allow partial/best-effort withdrawal of the non-frozen asset rather than reverting the whole atomic operation, or provide an escape hatch that lets LPs redeem the non-frozen asset independently.

### Proof of Concept
1. Attacker calls `pallet_assets::create(origin, id=X, admin=attacker, min_balance=1)` — attacker becomes issuer/admin/freezer of `X`.
2. Attacker mints `X` to self and calls `pallet_asset_conversion::create_pool(origin, Native, X)`.
3. Victim calls `add_liquidity(origin, Native, X, amount1, amount2, ..., victim)`, depositing native tokens and `X` into `pool_account`, receiving LP tokens.
4. Attacker calls `pallet_assets::freeze(origin, id=X, who=pool_account)` (or `freeze_asset`).
5. Victim calls `remove_liquidity(...)`: LP tokens burn succeeds, first `T::Assets::transfer` (native) may succeed, but the second `T::Assets::transfer` for `X` fails with `Token::Frozen` per `can_decrease` — the whole extrinsic reverts, so even the LP token burn is undone and the victim cannot redeem *any* of their native-token liquidity as long as `X` stays frozen.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L853-856)
```rust
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
