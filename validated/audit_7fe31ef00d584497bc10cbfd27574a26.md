### Title
`pallet-asset-conversion` liquidity pools use raw live asset balances as reserves, letting an unprivileged donor inflate the mint ratio and cause victim deposits to be diluted or rejected - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
The `ConicPool` report's core broken invariant is: an exchange-rate/ratio used to price new deposits is derived from a *live, externally-writable balance* rather than from an internally-accounted reserve that only changes on authorized deposit/withdraw paths. Sending tokens directly to the pool contract skews the ratio for the next depositor. The same pattern exists in `pallet-asset-conversion`, where `reserve1`/`reserve2` used for LP-token minting math are read directly from the pool account's live asset balance via `get_balance`, with no internal reserve bookkeeping that is reconciled only through `do_add_liquidity`/`do_remove_liquidity`. [1](#0-0) 

### Finding Description
`do_add_liquidity` computes the reserves for a pair by directly querying the pool account's asset balance: [2](#0-1) 

and, when the pool already has liquidity, mints LP tokens proportionally to these live reserves and the current LP `total_supply`: [3](#0-2) 

Because `get_balance` simply calls `T::Assets::balance(asset, owner)` [4](#0-3) , any account can inflate `reserve1` or `reserve2` for an existing pool by directly transferring the underlying `asset1`/`asset2` to the pool's deterministic `pool_account` (obtainable from `T::PoolLocator::address(&pool_id)`) — completely outside of `add_liquidity`/`remove_liquidity`, with no LP tokens minted for that donation. This is functionally identical to the ConicPool exploit of sending tokens straight to the vault to distort `exchangeRate`.

After such a donation, the next legitimate depositor's minted LP amount is computed as `min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)` [5](#0-4) . With an attacker-inflated reserve, this ratio can be driven arbitrarily low, so a normal-sized deposit yields a disproportionately small `lp_token_amount`, silently donating value to existing LP holders (the attacker included) at withdrawal time, or reverting the deposit entirely once the amount rounds at/below `MintMinLiquidity`: [6](#0-5) 

The `MintMinLiquidity` burn only protects the *very first* mint from the classic zero-supply inflation attack (`calc_lp_amount_for_zero_supply`) [7](#0-6) ; it does nothing to stop post-genesis reserve inflation via direct balance donation to an existing pool, because reserves for existing pools are read live rather than tracked as pallet storage that is only mutated by `do_add_liquidity`/`do_remove_liquidity`/`credit_swap`.

### Impact Explanation
This breaks the "conserve value / settle exactly once to the rightful beneficiary and amount" invariant for liquidity providers: an unprivileged attacker can permanently skew a pool's priced reserves without minting LP tokens, causing subsequent depositors to receive far fewer LP tokens than the fair share of their contribution (value transferred to existing/attacker LP holdings), or causing their `add_liquidity` calls to revert with `InsufficientLiquidityMinted`, degrading pool usability. Because swap pricing (`get_amount_out`/`get_amount_in`, `quote`) also derives from the same live-balance reserves [8](#0-7) [9](#0-8) , the same donation vector also directly distorts swap execution prices for all pool users.

### Likelihood Explanation
No privileged role, governance action, or malicious peer/validator is required — a plain, unprivileged `transfer` of the pool's underlying asset to the deterministic pool account (computable off-chain from `T::PoolLocator::address`) is sufficient. It does not require front-running a specific victim transaction; the attacker can donate at any time to permanently distort the pool's reserve accounting until enough organic volume normalizes it.

### Recommendation
Track pool reserves as pallet storage state (mutated only inside `do_add_liquidity`, `do_remove_liquidity`, and `credit_swap`) rather than reading the live asset balance of the pool account in `get_balance`/`get_reserves`. Alternatively, reconcile any balance in excess of the tracked reserve into the LP token supply (a "skim"/"sync" function) so donations cannot silently skew the mint/swap ratio, mirroring how Uniswap V2 separates its `reserve0`/`reserve1` state variables from live token balances.

### Proof of Concept
1. Attacker (or anyone) creates a pool via `create_pool(asset1, asset2)` and adds a modest amount of initial liquidity via `add_liquidity`, receiving `total_supply` LP tokens (see `do_create_pool`/`do_add_liquidity`) [10](#0-9) .
2. Attacker computes the pool account address via `T::PoolLocator::address(&pool_id)` and issues a direct `Assets::transfer`/`Balances::transfer` of a large amount of `asset1` (or the native token) straight to that account — bypassing `add_liquidity` entirely. This inflates `reserve1` returned by `get_balance` with no change to LP `total_supply`.
3. A victim calls `add_liquidity` with a normal deposit amount. `do_add_liquidity` computes `side1 = amount1 * total_supply / reserve1` using the now-inflated `reserve1`, producing a minted `lp_token_amount` far below the victim's fair share (or triggering `Error::<T>::InsufficientLiquidityMinted` if it rounds to ≤ `MintMinLiquidity`) [3](#0-2) .
4. The victim's deposited tokens (asset1/asset2) sit in the pool account backing the existing LP token supply; the attacker can call `remove_liquidity` to redeem their original LP tokens for a share that now includes the victim's undercompensated contribution.

*Note: I was unable to execute this against a running test harness within this session; the finding is based on static analysis of `do_add_liquidity`, `get_balance`, and `get_reserves` in `substrate/frame/asset-conversion/src/lib.rs`. A background Devin session with repo/test access would be needed to write and run a concrete `#[test]` reproducing the exact minted-LP delta.*

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L726-892)
```rust
		/// Create a new liquidity pool.
		///
		/// **Warning**: The storage must be rolled back on error.
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1343-1351)
```rust
		/// Calculates the optimal amount from the reserves.
		pub fn quote(
			amount: &T::Balance,
			reserve1: &T::Balance,
			reserve2: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			// (amount * reserve2) / reserve1
			Self::mul_div(amount, reserve2, reserve1)
		}
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1388-1419)
```rust
		pub fn get_amount_out(
			fee: Permill,
			amount_in: &T::Balance,
			reserve_in: &T::Balance,
			reserve_out: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount_in = T::HigherPrecisionBalance::from(*amount_in);
			let reserve_in = T::HigherPrecisionBalance::from(*reserve_in);
			let reserve_out = T::HigherPrecisionBalance::from(*reserve_out);

			if reserve_in.is_zero() || reserve_out.is_zero() {
				return Err(Error::<T>::ZeroLiquidity);
			}

			let fee_complement = fee.left_from_one().deconstruct();
			let amount_in_with_fee = amount_in
				.checked_mul(&T::HigherPrecisionBalance::from(fee_complement))
				.ok_or(Error::<T>::Overflow)?;

			let numerator =
				amount_in_with_fee.checked_mul(&reserve_out).ok_or(Error::<T>::Overflow)?;

			let denominator = reserve_in
				.checked_mul(&T::HigherPrecisionBalance::from(Permill::ACCURACY))
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&amount_in_with_fee)
				.ok_or(Error::<T>::Overflow)?;

			let result = numerator.checked_div(&denominator).ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```
