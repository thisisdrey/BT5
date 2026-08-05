### Title
Sole liquidity provider can never fully withdraw the pool's dust-locked reserve - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion`'s `do_remove_liquidity` enforces a hard minimum-reserve invariant on both pool assets after every withdrawal. Because the pool's `MintMinLiquidity` LP tokens are permanently locked to the pool account (never burnable) and the remaining reserve after any withdrawal must stay `>= T::Assets::minimum_balance(...)`, the last/sole liquidity provider of a pool can never fully exit their position. Their final "dust" share of the reserves becomes permanently stranded in the pool with no path to recovery — the same broken invariant class as the reported Salty issue where `pools.removeLiquidity()`'s DUST check made the sole USDS borrower's collateral impossible to withdraw/liquidate.

### Finding Description
`do_remove_liquidity` at [1](#0-0)  computes the withdrawable amounts proportionally to `lp_token_burn` versus `total_supply`, then requires:

```rust
let reserve1_left = reserve1.saturating_sub(amount1);
let reserve2_left = reserve2.saturating_sub(amount2);
ensure!(reserve1_left >= T::Assets::minimum_balance(asset1.clone()), Error::<T>::ReserveLeftLessThanMinimal);
ensure!(reserve2_left >= T::Assets::minimum_balance(asset2.clone()), Error::<T>::ReserveLeftLessThanMinimal);
```

There is no exception allowing `reserve*_left == 0` when the caller is redeeming effectively all real (non-locked) liquidity — unlike, e.g., Uniswap V2 semantics where a pool can be fully drained to zero and later re-seeded. On pool creation, `T::MintMinLiquidity::get()` LP tokens are permanently minted to the pool account itself and are never redeemable by any user [2](#0-1) , so `total_supply` always exceeds the LP tokens actually held by real users. Combined with the strict `>= minimum_balance` reserve floor, a sole liquidity provider (or the last remaining provider after everyone else exits) can withdraw only up to the point where reserves hit `minimum_balance(asset1)`/`minimum_balance(asset2)`, but never below. Any attempt to redeem enough LP tokens to fully exit reverts with `Error::ReserveLeftLessThanMinimal` [3](#0-2) . Since `T::Assets::minimum_balance` is generally non-zero for standard assets/native currency, this dust amount (proportional to the LP's final share) is permanently locked with no mechanism (no destroy-pool path, no force-remove, no admin override) to recover it, exactly mirroring the "cannot reduce reserves to zero to allow the required action" root cause in the external report, except here the trapped party is the LP themselves rather than a liquidator.

### Impact Explanation
This falls under "permanent user-fund ... lock": legitimate value belonging to the sole/last liquidity provider of any `pallet-asset-conversion` pool becomes permanently unrecoverable. There is no privileged or governance action, no malicious actor, and no off-chain assumption required — it's a direct consequence of the pallet's own invariant check colliding with its own locked-LP-token design. Any AMM pool instantiated from this pallet (used broadly across Polkadot SDK-based parachains for asset swaps) is affected once liquidity providers exit down to one remaining holder.

### Likelihood Explanation
High likelihood in practice: any pool that starts with multiple LPs will eventually converge to a single remaining LP as others exit (a common, expected lifecycle event, not an edge case). No coordination or adversarial setup is needed — it happens under completely normal usage whenever all-but-one LP withdraw and the last LP tries to fully exit.

### Recommendation
Allow `reserve1_left`/`reserve2_left` to reach `0` (or the locked `MintMinLiquidity` equivalent) when the redeemer is burning all of their externally-held LP tokens, e.g. by changing the check to `reserve_left >= minimum_balance || reserve_left.is_zero()`, or by tracking/allowing full pool teardown so the pool can be re-created from zero afterward (mirroring the add_liquidity zero-reserve reinitialization branch already present in `add_liquidity`). Alternatively, provide the pool account's locked `MintMinLiquidity` as a redeemable buffer only at the point of complete pool closure so the last real LP can retrieve their entire remaining balance.

### Proof of Concept
1. Create a pool for `(native, asset_id=2)` and add liquidity as the sole depositor; `MintMinLiquidity` LP tokens are minted to the pool account and the depositor receives `total_supply - MintMinLiquidity` LP tokens, per `add_liquidity`'s zero-supply branch [4](#0-3) .
2. Call `remove_liquidity` with `lp_token_burn` equal to the depositor's entire LP balance.
3. `do_remove_liquidity` computes `amount1`/`amount2` proportional to `lp_redeem_amount / total_supply`, leaving `reserve1_left`/`reserve2_left` strictly greater than zero (because `total_supply` includes the un-burnable `MintMinLiquidity`).
4. If either `reserve*_left` falls below `T::Assets::minimum_balance(asset*)`, the extrinsic reverts with `Error::ReserveLeftLessThanMinimal` [3](#0-2) , and there is no other call the sole LP can make to retrieve the remaining dust — it is permanently locked in the pool account.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-879)
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

			T::PoolAssets::mint_into(pool.lp_token.clone(), mint_to, lp_token_amount)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L894-939)
```rust
		/// Remove liquidity from a pool.
		pub(crate) fn do_remove_liquidity(
			who: &T::AccountId,
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			lp_token_burn: T::Balance,
			amount1_min_receive: T::Balance,
			amount2_min_receive: T::Balance,
			withdraw_to: &T::AccountId,
		) -> Result<(T::Balance, T::Balance), DispatchError> {
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			ensure!(lp_token_burn > Zero::zero(), Error::<T>::ZeroLiquidity);

			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;

			ensure!(
				!amount1.is_zero() && amount1 >= amount1_min_receive,
				Error::<T>::AssetOneWithdrawalDidNotMeetMinimum
			);
			ensure!(
				!amount2.is_zero() && amount2 >= amount2_min_receive,
				Error::<T>::AssetTwoWithdrawalDidNotMeetMinimum
			);
			let reserve1_left = reserve1.saturating_sub(amount1);
			let reserve2_left = reserve2.saturating_sub(amount2);
			ensure!(
				reserve1_left >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::ReserveLeftLessThanMinimal
			);
			ensure!(
				reserve2_left >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::ReserveLeftLessThanMinimal
			);
```
