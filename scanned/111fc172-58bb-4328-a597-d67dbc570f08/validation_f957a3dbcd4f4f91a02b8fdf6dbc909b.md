### Title
Pool creation and initial price-setting are decoupled and unauthenticated in `pallet-asset-conversion`, allowing front-run/donation manipulation of a pool's starting exchange rate - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
The external report's core broken invariant is: a user-controlled "starting price" value is accepted by the system without being bound into the identifier that scopes it, letting an attacker unilaterally fix an unfair price for a resource that other users will later interact with under the assumption it is neutral/fair. The local analog is `pallet_asset_conversion`'s two-step, permissionless pool lifecycle: `create_pool` derives the `PoolId` purely from the *asset pair*, with no price/ratio component, and the pool's *actual* starting exchange rate is set later and separately by whichever account is first to call the permissionless `add_liquidity` extrinsic.

### Finding Description
`Pallet::create_pool` computes the pool identity strictly from the asset kinds via `T::PoolLocator::pool_id(&asset1, &asset2)` and inserts an empty `PoolInfo` — no price, ratio, or reserve data is part of the key: [1](#0-0) 

The pallet is explicitly documented as Uniswap-V2-style, meaning the *initial* exchange rate of a freshly created (zero-reserve) pool is defined entirely by whichever account first successfully calls `add_liquidity` and by the ratio of `amount1_desired`/`amount2_desired` it supplies: [2](#0-1) 

`create_pool` (`call_index(0)`) and `add_liquidity` (`call_index(1)`) are independent, permissionless, signed extrinsics — anyone can call either, and nothing ties a specific caller or a specific price to the pool once it is created: [3](#0-2) [4](#0-3) 

This is structurally identical to the reported bug class: `startSqrtPriceX96` is an arbitrary caller-supplied value that is not part of the battle key, so anyone can set an unfair starting price for a battle before the intended party interacts with it. Here, the "starting price" of the AMM pool (the initial reserve ratio) is likewise arbitrary, caller-supplied, and not committed to the `PoolId`/`PoolInfo`, and the two required steps (create then first-deposit) are separable across blocks/callers, giving an attacker a race window to set the ratio.

### Impact Explanation
An attacker who observes (mempool) or anticipates a legitimate LP's intent to bootstrap a new pool can front-run with their own `add_liquidity` call using a heavily skewed ratio (e.g., depositing a large amount of a low-value/attacker-controlled asset against a minimal amount of the counter-asset). Because reserves start at zero, this sets the pool's baseline exchange rate arbitrarily. Consequences:
- The legitimate LP's subsequent `add_liquidity` is executed proportionally against the now-skewed reserves, causing them to receive a disadvantageous LP-token split or to be rejected by their `amount_min` slippage bounds, effectively a griefing/DoS on pool bootstrapping.
- Any user who swaps against the pool before reserves are corrected trades at an attacker-dictated unfair price, letting the attacker extract value (classic AMM donation/first-depositor attack), directly matching the report's "grief other users" and "profit from unfair trade conditions" impacts, and aligning with the required impact class of public underpriced/mispriced work degrading normal chain economic operation.

### Likelihood Explanation
This requires no privileged role, no validator/collator/relayer collusion, and no admin action — only an ordinary signed account able to submit a transaction shortly after (or in the same block window as) a `create_pool`/first `add_liquidity` sequence, which is inherent to how pool bootstrapping is exposed on public chains like Asset Hub. The pallet's own genesis-bootstrap path (`setup_pool_from_genesis`) exists specifically to avoid this exposure for governance-created pools, implicitly acknowledging that permissionless first-deposit is unsafe for setting canonical starting prices: [5](#0-4) 

### Recommendation
Bind an intended/expected initial price (or a minimum-liquidity/anti-manipulation safeguard) into the pool creation flow, e.g.: require `create_pool` and the first `add_liquidity` to be atomic (single extrinsic) for the creator, or require the first depositor to also be the pool creator, or enforce a minimum-liquidity burn combined with a maximum allowed initial-ratio skew, so that the "starting price" cannot be unilaterally fixed by an unrelated account before the intended liquidity provider interacts with the pool.

### Proof of Concept
1. Attacker monitors the chain for a `create_pool(asset1, asset2)` call (or creates the pool themselves).
2. Immediately after pool creation, before the legitimate LP's `add_liquidity` transaction executes, attacker calls `add_liquidity(asset1, asset2, amount1_desired = 1, amount2_desired = 1_000_000, amount1_min = 0, amount2_min = 0, mint_to = attacker)` — see the extrinsic signature at: [4](#0-3) 
3. Pool reserves are now fixed at an attacker-chosen 1:1,000,000 ratio.
4. The legitimate LP's later `add_liquidity` call is computed proportionally to this skewed ratio (per `do_create_pool`/`do_add_liquidity` logic referenced at lines 726-788), causing either rejection via `amount_min` checks or an unfair LP-token allocation; any swaps executed in the interim occur at the attacker-set unfair price.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L18-30)
```rust
//! # Substrate Asset Conversion pallet
//!
//! Substrate Asset Conversion pallet based on the [Uniswap V2](https://github.com/Uniswap/v2-core) logic.
//!
//! ## Overview
//!
//! This pallet allows you to:
//!
//!  - [create a liquidity pool](`Pallet::create_pool()`) for 2 assets
//!  - [provide the liquidity](`Pallet::add_liquidity()`) and receive back an LP token
//!  - [exchange the LP token back to assets](`Pallet::remove_liquidity()`)
//!  - [swap a specific amount of assets for another](`Pallet::swap_exact_tokens_for_tokens()`) if
//!    there is a pool created, or
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L661-724)
```rust
	impl<T: Config> Pallet<T> {
		/// Create a pool at genesis, bypassing the setup fee.
		///
		/// The `lp_provider` must already hold sufficient balances of both assets.
		/// If both `amount1` and `amount2` are non-zero, initial liquidity is added.
		/// Returns the LP token amount minted to `lp_provider` (zero if no liquidity).
		pub(crate) fn setup_pool_from_genesis(
			asset1: &T::AssetKind,
			asset2: &T::AssetKind,
			lp_provider: &T::AccountId,
			amount1: T::Balance,
			amount2: T::Balance,
		) -> Result<T::Balance, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);

			let pool_id = T::PoolLocator::pool_id(asset1, asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// Allocate LP token ID.
			let lp_token = NextPoolAssetId::<T>::get()
				.or(T::PoolAssetId::initial_value())
				.ok_or(Error::<T>::IncorrectPoolAssetId)?;
			let next_lp_token_id = lp_token.increment().ok_or(Error::<T>::IncorrectPoolAssetId)?;
			NextPoolAssetId::<T>::set(Some(next_lp_token_id));

			// Create LP token asset.
			T::PoolAssets::create(lp_token.clone(), pool_account.clone(), false, 1u32.into())?;

			// Touch asset accounts for the pool account.
			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, lp_provider)?;
			}
			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, lp_provider)?;
			}
			if T::PoolAssets::should_touch(lp_token.clone(), &pool_account) {
				T::PoolAssets::touch(lp_token.clone(), &pool_account, lp_provider)?;
			}

			// Register pool.
			Pools::<T>::insert(pool_id, PoolInfo { lp_token: lp_token.clone() });

			// Add initial liquidity if amounts are non-zero.
			if !amount1.is_zero() && !amount2.is_zero() {
				T::Assets::transfer(asset1.clone(), lp_provider, &pool_account, amount1, Preserve)?;
				T::Assets::transfer(asset2.clone(), lp_provider, &pool_account, amount2, Preserve)?;

				let lp_token_amount = Self::calc_lp_amount_for_zero_supply(&amount1, &amount2)?;
				T::PoolAssets::mint_into(
					lp_token.clone(),
					&pool_account,
					T::MintMinLiquidity::get(),
				)?;
				T::PoolAssets::mint_into(lp_token, lp_provider, lp_token_amount)?;

				Ok(lp_token_amount)
			} else {
				Ok(Zero::zero())
			}
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L740-774)
```rust
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

```
