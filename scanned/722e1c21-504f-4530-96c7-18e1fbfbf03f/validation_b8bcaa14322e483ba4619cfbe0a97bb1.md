### Title
Permissionless-asset pairing lets a frozen/malicious token permanently lock LP principal in `pallet-asset-conversion::remove_liquidity` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` lets anyone pair two arbitrary `T::AssetKind`s into a pool via `create_pool`/`add_liquidity` [1](#0-0) . When a liquidity provider wants their principal back, `do_remove_liquidity` burns the LP token and then requires **both** asset legs of the pool to transfer successfully in sequence before the call can return `Ok` [2](#0-1) . If one of the two paired assets is (or later becomes) untransferable — e.g. it is a `pallet-assets` asset that its own (permissionless) asset admin/freezer can freeze, or any `T::AssetKind` implementation whose `Mutate::transfer` can be made to always fail for the pool account — the second `T::Assets::transfer` call in `do_remove_liquidity` will always fail, and the whole extrinsic (LP-burn included) is discarded by FRAME's automatic per-dispatchable storage layer. The net effect: the LP holder's principal in that pool is permanently unredeemable through the only exit path (`remove_liquidity`), because there is no "single-asset" or "emergency" withdrawal path that ignores a stuck leg.

### Finding Description
This is the direct on-chain analog of the FactoryDAO bug: a withdrawal path that bundles a user's own principal redemption together with a transfer that a third party (the counter-asset's issuer/freezer, who is permissionless to register) fully controls, with an all-or-nothing execution model and no bypass.

- `create_pool` accepts any two `T::AssetKind` values from any signed origin, no admin approval needed [1](#0-0) . Anyone (including an attacker) can register an asset via `pallet-assets` and pair it with a valuable asset (e.g. the native token) to form a pool that unsuspecting LPs might add liquidity to.
- `pallet-assets` gives the asset's own team (`Freezer`/`Admin`, set at asset creation, which is itself permissionless) the ability to freeze balances/accounts of that asset (`freeze`/`freeze_asset` functionality is visible in `substrate/frame/assets/src/lib.rs`, confirmed present via grep matches for `Frozen`/`freeze_asset`/`thaw_asset` in that file). This is not a chain-level governance or validator privilege — it is an ordinary permissionless asset-creator capability, matching the "malicious pool creator" role in the original report rather than a disallowed "admin/governance" actor.
- `do_remove_liquidity` performs, in order: burn LP tokens from `who` → `T::Assets::transfer(asset1, ...)` → `T::Assets::transfer(asset2, ...)` [2](#0-1) . There is no fallback that lets the LP redeem only the healthy asset leg, and no code path that tolerates one leg's transfer failing (unlike, e.g., a design that would credit failed transfers to a claimable balance).
- Because `#[pallet::call]` dispatchables are automatically wrapped in `with_storage_layer`, a failure anywhere in `do_remove_liquidity` rolls back the entire extrinsic, including the LP-token burn [3](#0-2) [4](#0-3) . This is good — it prevents fund *loss* from partial execution — but it does nothing to prevent the fund *lock*: the LP token is never destroyed, but the corresponding principal can never actually be moved out of the pool as long as the frozen/malicious asset transfer keeps failing.
- Existing guards (`ReserveLeftLessThanMinimal`, `AssetOneWithdrawalDidNotMeetMinimum`, `ZeroLiquidity`, min-balance checks) all validate amounts and pool-level ED constraints; none of them validate that both `T::Assets::transfer` calls are actually guaranteed to succeed for a possibly frozen/malicious asset, and none provide an alternate exit.

### Impact Explanation
Any liquidity provider who adds liquidity to a pool containing an asset that its issuer later freezes (or an asset that is deliberately designed to always fail transfers from a specific account, e.g. the pool account) permanently loses access to their principal in that pool. This is a "permanent user-fund lock" impact explicitly in scope ("permanent user-fund or bridge-state lock"). Because pool creation and asset registration are both permissionless, this can be executed by an ordinary unprivileged user acting as an asset creator — no validator, collator, governance, or relayer compromise is required. The LP is not "stolen" (no beneficiary receives it), but it becomes permanently inaccessible, which is the exact class of harm the FactoryDAO report describes ("unaware users will have their funds stuck").

### Likelihood Explanation
Medium-High. The attack requires only: (1) permissionlessly creating an asset with freeze capability (standard `pallet-assets::create`/`force_create` workflow), (2) creating a pool pairing that asset with a desirable asset via `create_pool`, (3) waiting for unaware LPs to `add_liquidity`, then (4) freezing the asset (or the pool account specifically) at the attacker's discretion. No race condition, no front-running, and no reliance on any other party's mistake beyond a normal LP interacting with what looks like a legitimate pool — mirroring the "pool creator relies on social engineering to funnel unsuspecting users" pattern the original report's judge described as the umbrella root cause.

### Recommendation
- Add an `emergencyWithdraw`/partial-redemption path for `remove_liquidity` that allows an LP to redeem the healthy asset leg (or a pro-rata amount) even when the other leg's transfer fails, mirroring the report's exact recommendation for FactoryDAO.
- Alternatively/additionally, decouple the LP-token burn from the two transfers so that a failure on one leg can result in partial success (burn only the fraction actually paid out) rather than an all-or-nothing outcome, or credit the un-transferable amount to a claimable/held balance the user can retrieve once the asset is unfrozen.
- Consider disallowing/flagging pools whose `AssetKind` supports third-party freeze/block hooks that can target the pool account specifically, or require `Assets::can_withdraw`/dry-run checks before allowing `add_liquidity` into such assets.

### Proof of Concept
1. Attacker calls `pallet_assets::create` (or the runtime's asset-creation extrinsic) to register `AssetX`, setting themselves as `Freezer`/`Admin`.
2. Attacker calls `pallet_asset_conversion::create_pool(Native, AssetX)` [1](#0-0)  and adds a small amount of liquidity to make the pool appear legitimate.
3. Victim calls `add_liquidity(Native, AssetX, ...)`, receiving LP tokens for the pool [5](#0-4) .
4. Attacker (as `AssetX`'s freezer) freezes the pool account's `AssetX` balance (or freezes the whole asset) using `pallet_assets`'s freeze functionality.
5. Victim calls `remove_liquidity(Native, AssetX, lp_token_burn, ...)`. `do_remove_liquidity` burns the LP tokens locally, successfully transfers the `Native` leg, then fails on `T::Assets::transfer(AssetX, &pool_account, withdraw_to, amount2, Expendable)` [6](#0-5) .
6. The extrinsic reverts atomically (LP tokens are not actually burned thanks to `with_storage_layer`), but the victim can never successfully call `remove_liquidity` again as long as `AssetX` remains frozen for the pool account — their principal is permanently locked with no alternative exit path in the pallet.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L442-450)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L941-953)
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

**File:** substrate/frame/support/src/lib.rs (L1675-1680)
```rust
	///
	/// The macro also ensures that the extrinsic when invoked will be wrapped via
	/// [`frame_support::storage::with_storage_layer`] to make it transactional. Thus if the
	/// extrinsic returns with an error any state changes that had already occurred will be
	/// rolled back.
	///
```

**File:** substrate/frame/support/src/storage/transactional.rs (L183-201)
```rust
/// Execute the supplied function, adding a new storage layer.
///
/// This is the same as `with_transaction`, but assuming that any function returning an `Err` should
/// rollback, and any function returning `Ok` should commit. This provides a cleaner API to the
/// developer who wants this behavior.
pub fn with_storage_layer<T, E, F>(f: F) -> Result<T, E>
where
	E: From<DispatchError>,
	F: FnOnce() -> Result<T, E>,
{
	with_transaction(|| {
		let r = f();
		if r.is_ok() {
			TransactionOutcome::Commit(r)
		} else {
			TransactionOutcome::Rollback(r)
		}
	})
}
```
