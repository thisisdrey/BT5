## Analysis

The external report's core broken invariant: a share-minting ratio is computed from a value (vault asset balance) that can be inflated by direct token transfer, independent of the accounting path (`deposit`) that mints shares. This lets an attacker desync "shares outstanding" from "assets actually owed," stealing value from later depositors.

I traced this pattern through `pallet-nomination-pools` (bonded pool `points`/`balance` ratio) and `pallet-asset-conversion` (AMM LP shares). The nomination-pools bonded-pool ratio is **not** vulnerable to this class because `balance_to_point`/`points_to_balance` derive the pool's "balance" from `T::StakeAdapter::active_stake(...)` [1](#0-0) , which is the staking pallet's internally tracked bonded ledger — a raw token transfer to the pool's bonded account does not change `active_stake`, so the classic donation-inflation vector is closed there.

`pallet-asset-conversion`, however, reproduces the vulnerable pattern: it computes pool reserves directly from live account balances rather than from an internally tracked, donation-immune reserve counter.

### Title
AMM pool reserves computed from live account balance allow donation-based share/price manipulation - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion`'s `do_add_liquidity`, `do_remove_liquidity`, and swap paths derive pool reserves via `Self::get_balance(&pool_account, asset)` / `Self::get_reserves(...)`, i.e., the *actual* live token balance held by the pool's sovereign account [2](#0-1) . Because any account can transfer tokens directly to the deterministic `pool_account` (derived via `T::PoolLocator::address`) without going through `add_liquidity`, an attacker can inflate one side of the reserve independent of the LP-token supply, corrupting the price/ratio used for both liquidity minting and swap output/input calculations.

### Finding Description
- Pool reserves are not tracked as an internal, donation-resistant counter (e.g., a `reserve1`/`reserve2` field updated only on `mint`/`burn`/`swap`); they are read live from asset balances of the pool account: `Self::get_balance(&pool_account, asset1.clone())` / `asset2.clone()` [2](#0-1)  and via `Self::get_reserves` used identically in `do_remove_liquidity` [3](#0-2) .
- `do_add_liquidity` computes the LP tokens to mint proportionally to `reserve1`/`reserve2` when `total_supply` is nonzero: `side1 = amount1 * total_supply / reserve1`, `side2 = amount2 * total_supply / reserve2`, taking the minimum [4](#0-3) .
- The pallet does implement the standard Uniswap-V2-style first-deposit fix (`MintMinLiquidity` permanently minted to the pool account and `lp_token_amount > MintMinLiquidity` check) [5](#0-4) . This defends against the *very first* deposit inflation described in the report.
- However, this defense only guards the moment `total_supply == 0`. At any later point, an attacker can still directly transfer (donate) `asset1` or `asset2` into `pool_account` outside of `add_liquidity`, changing `reserve1`/`reserve2` without minting any LP tokens and without changing `total_supply`. This desynchronizes the "reserve implied by LP supply" from "actual balance," which:
  1. Corrupts subsequent `add_liquidity` mint ratios — a victim depositing based on an expected quote can be minted disproportionately few LP tokens for the assets they contribute (their contribution effectively subsidizes existing LP holders, mirroring the report's "Bob deposits 19e18, gets diluted" scenario), and
  2. Corrupts swap pricing (`get_amount_out`/`get_amount_in`, `quote`) since these are computed from the same live-balance reserves, letting the attacker manipulate slippage/price for a subsequent swap in the same block (e.g., combined with `remove_liquidity` to extract the manipulated balance back), then reset reserves.
- Existing guards (`MintMinLiquidity`, `AmountOneLessThanMinimal`/`AmountTwoLessThanMinimal`, `ReserveLeftLessThanMinimal`) only check that reserves stay above the asset's existential/minimum balance — they do not detect or prevent a reserve balance that is inflated by unaccounted external donations, since the pallet treats `get_balance(pool_account, ...)` as ground truth at all times, not just at pool creation.

### Impact Explanation
This affects the core AMM value-conservation invariant in a live-scope pallet used by Asset Hub / parachain runtimes (`pallet-asset-conversion`, wired into `NativeAndAssets`/`PoolAssets` per the Penpal runtime config [6](#0-5) ). An unprivileged actor with no special privileges can donate tokens to a pool's sovereign account to skew reserves and cause an honest liquidity provider to receive a wrong (too-small) LP-token allocation relative to the value they contributed, or to manipulate the effective swap price experienced by other users in the same block — both are "wrong beneficiary or amount"/fund-loss style impacts against ordinary users, not requiring a malicious validator/relayer/admin.

### Likelihood Explanation
Likelihood is moderate: the attacker needs (a) knowledge of the deterministic pool account address (computable from `PoolId`/`PoolLocator`, public), (b) enough capital to donate and later recoup via `remove_liquidity`/swap, and (c) transaction ordering advantage (mempool visibility or same-block composition via a batch/utility call) to sandwich a victim's `add_liquidity` or swap call. This is a well-known AMM design risk (reserve-vs-balance desync / "donation attack") that Uniswap V2 forks mitigate by maintaining `reserve0`/`reserve1` as separate storage updated only via `sync()`/mint/burn/swap, decoupled from live `balanceOf`. `pallet-asset-conversion` does not maintain such separate reserve storage — it always reads live balances, so the exposure exists continuously, not just at genesis.

### Recommendation
- Introduce dedicated, pallet-tracked reserve fields per pool (updated only inside `do_add_liquidity`, `do_remove_liquidity`, and swap execution) instead of reading `T::Assets::balance(pool_account, ...)` live, mirroring Uniswap V2's `reserve0`/`reserve1` plus a `sync()`-style reconciliation gated behind explicit logic.
- If live balances must be used for compatibility, add a check comparing the live balance against the last-recorded reserve and reject/quarantine calls when an unexplained surplus is detected (or route any donated surplus to protocol/treasury rather than letting it skew per-provider math).
- Add regression tests that directly transfer assets to a pool's sovereign account between `add_liquidity` calls and assert LP-mint and swap-output amounts are unaffected.

### Proof of Concept
1. Create pool for `(NativeToken, AssetX)` and have victim intend to call `add_liquidity` with `amount1_desired = 10_000`, `amount2_desired = 10_000` expecting an ~1:1 quote based on current reserves (say `reserve1 = reserve2 = 10_000` after initial LP setup).
2. Attacker, before the victim's transaction executes (via mempool front-run or same-block ordering), directly transfers (not via `add_liquidity`) a large amount of `AssetX`, e.g. `+90_000`, to the pool's sovereign account computed via `T::PoolLocator::address(&pool_id)`.
3. Victim's `add_liquidity` executes: `reserve2` is now `100_000` while `reserve1` stays `10_000`; `Self::quote(&amount1_desired, &reserve1, &reserve2)` computed in `do_add_liquidity` [7](#0-6)  now yields a skewed `amount2_optimal`, causing the victim to either be forced to supply far more `AssetX` than intended or receive proportionally fewer LP tokens for their native-token contribution than the pre-donation price implied.
4. Attacker then calls `remove_liquidity` or executes a swap to extract the donated value back out of the pool at the now-corrected/rebalanced ratio, capturing part of the victim's excess contribution as profit — reproducing the report's "one depositor inflates price and gains more than deposited, other depositor gets less" outcome, using `do_remove_liquidity`'s proportional payout from `reserve1`/`reserve2` and `total_supply` [8](#0-7) .

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1064-1070)
```rust
	/// Convert the given amount of balance to points given the current pool state.
	///
	/// This is often used for bonding and issuing new funds into the pool.
	fn balance_to_point(&self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::balance_to_point(bonded_balance, self.points, new_funds)
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L822-843)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-877)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L911-920)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/lib.rs (L558-583)
```rust
impl pallet_asset_conversion::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Balance = Balance;
	type HigherPrecisionBalance = sp_core::U256;
	type AssetKind = xcm::latest::Location;
	type Assets = NativeAndAssets;
	type PoolId = (Self::AssetKind, Self::AssetKind);
	type PoolLocator = pallet_asset_conversion::WithFirstAsset<
		xcm_config::PenpalNativeCurrency,
		AccountId,
		Self::AssetKind,
		PoolIdToAccountId,
	>;
	type PoolAssetId = u32;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = ConstU128<0>; // Asset class deposit fees are sufficient to prevent spam
	type PoolSetupFeeAsset = xcm_config::PenpalNativeCurrency;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
	type LiquidityWithdrawalFee = LiquidityWithdrawalFee;
	type LPFee = LpFee;
	type PalletId = AssetConversionPalletId;
	type MaxSwapPathLength = ConstU32<3>;
	type MintMinLiquidity = ConstU128<100>;
	type AdminOrigin = AssetsForceOrigin;
	type MaxSwapFee = MaxSwapFee;
	type WeightInfo = ();
```
