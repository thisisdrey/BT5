### Title
Front-running donation before first `add_liquidity` inflates AMM pool reserves without minting LP tokens - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion` computes pool reserves and LP token mint amounts from the *live token balance* of the deterministic pool account rather than an internally tracked reserve variable. Because the pool account address is fully deterministic and publicly derivable as soon as `create_pool` is called, an unprivileged actor can transfer tokens directly into the pool account before the first legitimate `add_liquidity` call. This is the same "donation before first deposit" inflation-attack primitive described in the external `OzUSD` report, where `_getTotalPooledUSDX()`/share accounting relies on the contract's raw balance instead of an internally accounted value.

### Finding Description
`do_add_liquidity` reads reserves straight from asset balances: [1](#0-0) 

For a fresh pool (`total_supply.is_zero()`), the LP mint amount is computed purely from the transaction's own `amount1`/`amount2`, with no knowledge of whatever balance the pool account already holds: [2](#0-1) [3](#0-2) 

The pool account is deterministically derived from the asset pair right after `create_pool` succeeds and is a normal account, so any signed account can transfer tokens into it (e.g. `pallet-balances::transfer` / `pallet-assets::transfer`) before anyone calls `add_liquidity`: [4](#0-3) 

Because the branch selection for reserve-zero pools only checks `reserve1.is_zero() || reserve2.is_zero()` (not whether `total_supply` is zero, and not accounting for a pre-existing balance in only one of the two assets): [5](#0-4) 

an attacker can donate to only one side of the pair (leaving the other reserve at zero) so the "reserve-is-zero" branch is still taken for the legitimate first depositor, and the LP mint amount is computed from `amount1_desired`/`amount2_desired` only — ignoring the donated balance already sitting in the pool account. After this transaction, `get_balance(&pool_account, asset1)` (used by every subsequent `add_liquidity`/`remove_liquidity`/swap call) is inflated by the donated amount, while `T::PoolAssets::total_issuance(lp_token)` was minted only against the legitimate depositor's contribution. All later liquidity providers get their LP-token allocation computed against this inflated reserve via `mul_div(&amount1, &total_supply, &reserve1)`, so their true share of the pool is diluted/rounded down, and small deposits can round to zero and fail the `InsufficientLiquidityMinted` check (a DoS on `add_liquidity`).

The existing `MintMinLiquidity` guard only protects against the initial mint being degenerate (i.e., griefing the very first mint down to zero); it does not protect against a balance donation that is never matched by an LP-token mint, which is exactly the broken invariant identified in the external `OzUSD` report (raw balance used as "shares denominator" instead of an internally tracked value).

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for liquidity pools: donated funds are absorbed into pool reserves with no corresponding LP-token claim, degrading the precision and fairness of all subsequent `add_liquidity`/`remove_liquidity`/swap operations on that pool and potentially causing legitimate small deposits to be rejected (`InsufficientLiquidityMinted`), i.e., public underpriced/broken accounting affecting AMM economics on Asset Hub. It does not require any privileged, governance, validator, or off-chain actor — any signed account with tokens can perform it.

### Likelihood Explanation
Likelihood is bounded by: (1) the pool address is deterministic and public immediately after `create_pool`, so the attacker needs only to win a small race against the first `add_liquidity` call (front-run-only condition explicitly listed as normally out of scope, which weakens confidence this qualifies as a stand-alone accepted finding); and (2) the resulting damage is mostly griefing/precision-loss to future depositors and permanent loss of the attacker's own donated funds rather than a direct theft or duplicate settlement. This keeps the issue closer to a "front-run-only" class explicitly excluded by the impact gate, so it should be treated with caution despite the structural similarity to the `OzUSD` inflation bug.

### Recommendation
- Track pool reserves in dedicated storage (updated only through `add_liquidity`/`remove_liquidity`/swap paths) instead of reading the live token balance of the pool account, so unaccounted transfers cannot influence reserve/LP-mint math.
- Alternatively, on first liquidity provision, credit any pre-existing (unaccounted) balance in the pool account to the first minter, or reject `add_liquidity` if the pool account's balance does not match the expected pre-transaction state.
- Ensure the "reserve is zero" branch is gated on `total_supply.is_zero()` rather than solely on a single asset's reserve, closing the single-sided-donation front-loading path.

### Proof of Concept
1. Account `A` calls `create_pool(asset1, asset2)`; the pool account address is computed deterministically by `PoolLocator::address`.
2. Before anyone calls `add_liquidity`, `A` transfers a large amount of `asset1` directly into the pool account (e.g., via `pallet_assets::transfer`), while `asset2` balance of the pool account remains `0`.
3. Legitimate user `B` calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)`. Since `reserve2 == 0`, the code takes the `amount1 = amount1_desired; amount2 = amount2_desired` branch (`substrate/frame/asset-conversion/src/lib.rs:816-821`), and LP tokens are minted via `calc_lp_amount_for_zero_supply(&amount1, &amount2)` — using only `B`'s contributed amounts, not the actual `asset1` balance already sitting in the pool account.
4. Post-transaction, `get_balance(pool_account, asset1)` = donated amount + `B`'s `amount1`, but `total_issuance(lp_token)` reflects only `B`'s contribution. Any subsequent depositor's LP allocation, computed via `mul_div(&amount1, &total_supply, &reserve1)` (`substrate/frame/asset-conversion/src/lib.rs:869-871`), is diluted by the phantom donated reserve, and small deposits can fail `InsufficientLiquidityMinted` (`substrate/frame/asset-conversion/src/lib.rs:874-877`).

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L729-788)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-844)
```rust
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
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1353-1368)
```rust
		pub(super) fn calc_lp_amount_for_zero_supply(
			amount1: &T::Balance,
			amount2: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount1 = T::HigherPrecisionBalance::from(*amount1);
			let amount2 = T::HigherPrecisionBalance::from(*amount2);

			let result = amount1
				.checked_mul(&amount2)
				.ok_or(Error::<T>::Overflow)?
				.integer_sqrt()
				.checked_sub(&T::MintMinLiquidity::get().into())
				.ok_or(Error::<T>::InsufficientLiquidityMinted)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```
