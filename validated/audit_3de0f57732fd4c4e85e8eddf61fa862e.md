Audit Report

## Title
Destroying a pool asset via `Assets::start_destroy` permanently locks all liquidity providers' funds in `pallet-asset-conversion` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`pallet-asset-conversion::do_remove_liquidity` transfers pool reserves out of the pool account using `T::Assets::transfer(..., Expendable)` with no fallback path, while `pallet-assets::do_start_destroy` — callable by any asset's ordinary, non-privileged `owner` — flips the asset into `AssetStatus::Destroying`, causing every subsequent withdrawal consequence check on that asset to unconditionally return `WithdrawConsequence::UnknownAsset`. Once triggered on any asset used in a liquidity pool, LP token holders can never again call `remove_liquidity` for that pool, permanently locking their deposited reserves.

## Finding Description
`do_remove_liquidity` burns the caller's LP tokens and then unconditionally calls `T::Assets::transfer(asset1, &pool_account, withdraw_to, amount1, Expendable)` (and the analogous call for `asset2`) with no alternate/emergency withdrawal path: [1](#0-0) 

Every asset withdrawal, including this `transfer` call, is routed through `pallet-assets::functions::can_decrease`, which hard-codes an unconditional rejection returning `WithdrawConsequence::UnknownAsset` whenever the asset's status is `Destroying`, prior to any per-account or amount-specific checks: [2](#0-1) 

`do_start_destroy`, which sets `AssetStatus::Destroying`, is reachable via the `start_destroy` extrinsic by the asset's own `owner` — a non-privileged, permissionless role for any asset created via the standard `create`/`force_create` flow — and only guards against outstanding holds/freezes, never against balances held by other pallets (such as an `asset-conversion` pool account) referencing the asset as reserve collateral: [3](#0-2) [4](#0-3) 

The pallet's own test suite confirms that once `Destroying` is set, both `can_increase` and `can_decrease` return the "unknown asset" consequence for every account, regardless of caller or balance: [5](#0-4) 

Because Substrate dispatchables run in an implicit transactional context, when `transfer` fails inside `do_remove_liquidity`, the preceding `T::PoolAssets::burn_from` LP-token burn is rolled back too — the call simply errors out on every attempt, with no code path in `pallet-asset-conversion` to force/rescue the reserves (unlike, e.g., `pallet-bounties::reclaim_bounty_funds`, which sweeps stranded funds via a `Fortitude::Force`-style permissionless mechanism): [6](#0-5) 

## Impact Explanation
Any liquidity pool containing a non-native/permissionless asset is exposed: once that asset's owner calls the routine `start_destroy` extrinsic, every LP in every pool referencing that asset becomes permanently unable to redeem LP tokens for the underlying reserves — a durable, unrecoverable lock of user funds, matching the "permanent user-fund ... lock" impact category. No value is directly stolen by the asset owner, but the pool's LPs (third parties uninvolved in the `start_destroy` call) suffer irreversible loss of access to their assets.

## Likelihood Explanation
`start_destroy` is a standard, low-privilege, permissionless lifecycle extrinsic — any account that created an asset via `pallet-assets::create` is its non-privileged `owner` and can invoke it at will, with no visibility into or dependency on whether `pallet-asset-conversion` pools reference that asset. There is no cross-pallet check preventing this, and no rescue mechanism exists once destruction starts, making the scenario reachable under normal, non-adversarial asset-lifecycle management by an unprivileged actor using only public extrinsics.

## Recommendation
- Add a check in `do_start_destroy` that rejects (or defers) destruction while the asset still has a nonzero balance held by any registered `asset-conversion` pool account, analogous to the existing `ContainsHolds`/`ContainsFreezes` guards.
- Alternatively, add a permissionless sweep/rescue extrinsic to `pallet-asset-conversion`, similar to `pallet-bounties::reclaim_bounty_funds`, allowing LPs to force-withdraw pool reserves via a `Fortitude::Force` transfer once the underlying asset enters `Destroying`/`Destroyed` state.
- At minimum, document this cross-pallet risk so runtime integrators can add guards (e.g., via a custom `Freezer`/`Holder` implementation that treats pool-account balances as freezes).

## Proof of Concept
1. Asset owner creates asset `X` via `pallet-assets::create` (or `force_create`).
2. A user creates pool `(Native, X)` via `AssetConversion::create_pool` and adds liquidity via `AssetConversion::add_liquidity`, receiving LP tokens.
3. The owner of `X` calls `Assets::start_destroy(RuntimeOrigin::signed(owner), X)`; this succeeds because there are no holds/freezes on `X`, irrespective of the pool's balance.
4. The LP calls `AssetConversion::remove_liquidity(...)`. The call fails because `T::Assets::transfer(X, &pool_account, withdraw_to, amount, Expendable)` triggers `can_decrease`, which returns `WithdrawConsequence::UnknownAsset` due to `AssetStatus::Destroying`.
5. The LP's tokens and the pool's reserves of `X` are now permanently unreclaimable through any pallet-provided call; `pallet-asset-conversion` provides no sweep/rescue mechanism.

### Citations

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

**File:** substrate/frame/assets/src/functions.rs (L181-195)
```rust
	) -> WithdrawConsequence<T::Balance> {
		use WithdrawConsequence::*;
		let details = match Asset::<T, I>::get(&id) {
			Some(details) => details,
			None => return UnknownAsset,
		};
		if details.supply.checked_sub(&amount).is_none() {
			return Underflow;
		}
		if details.status == AssetStatus::Frozen {
			return Frozen;
		}
		if details.status == AssetStatus::Destroying {
			return UnknownAsset;
		}
```

**File:** substrate/frame/assets/src/functions.rs (L799-819)
```rust
	/// Start the process of destroying an asset, by setting the asset status to `Destroying`, and
	/// emitting the `DestructionStarted` event.
	pub(super) fn do_start_destroy(
		id: T::AssetId,
		maybe_check_owner: Option<T::AccountId>,
	) -> DispatchResult {
		Asset::<T, I>::try_mutate_exists(id.clone(), |maybe_details| -> Result<(), DispatchError> {
			let details = maybe_details.as_mut().ok_or(Error::<T, I>::Unknown)?;
			if let Some(check_owner) = maybe_check_owner {
				ensure!(details.owner == check_owner, Error::<T, I>::NoPermission);
			}

			ensure!(!T::Holder::contains_holds(id.clone()), Error::<T, I>::ContainsHolds);
			ensure!(!T::Freezer::contains_freezes(id.clone()), Error::<T, I>::ContainsFreezes);

			details.status = AssetStatus::Destroying;

			Self::deposit_event(Event::DestructionStarted { asset_id: id });
			Ok(())
		})
	}
```

**File:** substrate/frame/assets/src/lib.rs (L934-954)
```rust
		/// Start the process of destroying a fungible asset class.
		///
		/// `start_destroy` is the first in a series of extrinsics that should be called, to allow
		/// destruction of an asset class.
		///
		/// The origin must conform to `ForceOrigin` or must be `Signed` by the asset's `owner`.
		///
		/// - `id`: The identifier of the asset to be destroyed. This must identify an existing
		///   asset.
		///
		/// It will fail with either [`Error::ContainsHolds`] or [`Error::ContainsFreezes`] if
		/// an account contains holds or freezes in place.
		#[pallet::call_index(2)]
		pub fn start_destroy(origin: OriginFor<T>, id: T::AssetIdParameter) -> DispatchResult {
			let maybe_check_owner = match T::ForceOrigin::try_origin(origin) {
				Ok(_) => None,
				Err(origin) => Some(ensure_signed(origin)?),
			};
			let id: T::AssetId = id.into();
			Self::do_start_destroy(id, maybe_check_owner)
		}
```

**File:** substrate/frame/assets/src/tests.rs (L2182-2208)
```rust
#[test]
fn increasing_or_decreasing_destroying_asset_should_not_work() {
	build_and_execute(|| {
		use frame_support::traits::fungibles::Inspect;

		let admin = 1;
		let admin_origin = RuntimeOrigin::signed(admin);

		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 0, admin, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(1), 0, 1, 100));
		assert_eq!(Assets::balance(0, 1), 100);

		assert_eq!(Assets::can_deposit(0, &1, 10, Provenance::Extant), DepositConsequence::Success);
		assert_eq!(Assets::can_withdraw(0, &1, 10), WithdrawConsequence::<_>::Success);
		assert_eq!(Assets::can_increase(0, &1, 10, false), DepositConsequence::Success);
		assert_eq!(Assets::can_decrease(0, &1, 10, false), WithdrawConsequence::<_>::Success);

		assert_ok!(Assets::start_destroy(admin_origin, 0));

		assert_eq!(
			Assets::can_deposit(0, &1, 10, Provenance::Extant),
			DepositConsequence::UnknownAsset
		);
		assert_eq!(Assets::can_withdraw(0, &1, 10), WithdrawConsequence::<_>::UnknownAsset);
		assert_eq!(Assets::can_increase(0, &1, 10, false), DepositConsequence::UnknownAsset);
		assert_eq!(Assets::can_decrease(0, &1, 10, false), WithdrawConsequence::<_>::UnknownAsset);
	});
```

**File:** substrate/frame/bounties/src/lib.rs (L1047-1090)
```rust
		/// Reclaim funds stranded in a closed bounty's account back to the treasury.
		///
		/// Permissionless. Moves all remaining assets from a closed bounty's account back to the
		/// treasury in a single call. Which assets are swept depends on the `TransferAllAssets`
		/// configuration.
		///
		/// The call is free if funds were reclaimed and paid otherwise, so no-op calls cannot be
		/// used to grief the network. Emits `BountyFundsReclaimed` on success.
		///
		/// ## Complexity
		/// - O(A) where A is the number of relevant assets configured in `TransferAllAssets`.
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
```
