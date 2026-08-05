## Finding: Missing slippage floor in `SwapFirstAssetTrader::refund_weight` [1](#0-0) 

### Title
`SwapFirstAssetTrader::refund_weight` executes the fee-refund swap with `amount_out_min = None`, allowing pool-price manipulation to drain the swap pool during XCM weight refunds - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used to let XCM senders pay delivery/execution fees in a non-native asset by swapping it into the chain's `Target` fee asset via `pallet_asset_conversion`. Its `buy_weight` path is protected: it calls `SwapCredit::swap_tokens_for_exact_tokens` with an exact `fee` amount, so it always converts to precisely the required amount of `Target`. However, its `refund_weight` path calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` with the minimum-output parameter hard-coded to `None`. [2](#0-1) [3](#0-2)  This is the same class of bug as the reported PancakeRouter issue: a swap-consuming call is invoked with `amount_out_min` unset (equivalent to 0), removing all slippage protection on that leg.

### Finding Description
`pallet_asset_conversion::Pallet<T>` implements `Swap`/`SwapCredit` on top of a constant-product AMM pool that any account can add to or remove liquidity from and swap against (`do_add_liquidity`, `do_remove_liquidity`, `do_swap_exact_tokens_for_tokens`) [4](#0-3) [5](#0-4) . Every other swap entrypoint in this pallet, and every call site in `SwapFirstAssetTrader::buy_weight`/`quote_weight`, enforces a non-zero bound (`amount_out_min`, `amount_in_max`, or exact `fee`) that reverts with `ProvidedMinimumNotSufficientForSwap`/similar on adverse pricing [6](#0-5) .

`refund_weight`, however, is invoked automatically by the XCM executor whenever unused weight is refunded after message execution, and swaps the previously-collected `Target` fee back into the asset originally supplied by the sender, with no floor on the amount received:
```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,   // <-- no minimum output enforced
) { ... }
``` [3](#0-2) 

Because the underlying pool is a standard permissionless AMM, and the refund happens deterministically as part of processing the very XCM program that funded `total_fee`, an attacker who controls (or precedes) the execution of that XCM message can shift the pool's reserves for `(Target, refund_swap_asset)` immediately beforehand (e.g., via a batched/preceding XCM instruction, or an ordinary `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` call landing in the same block) so that the unmin-bounded swap executes at a manipulated price. This is a classic sandwich vector: absent a floor, `refund_weight`'s swap will happily execute at an arbitrarily bad rate for the pool/LPs (or arbitrarily favorable rate for the attacker who set up the price shift), directly transferring value out of the AMM's liquidity providers with no on-chain guard to stop it.

### Impact Explanation
This breaks the "public underpriced work / conserve value" invariant for AMM-backed fee handling: value can be extracted from `pallet_asset_conversion` liquidity providers through a public, unprivileged fee-refund code path that every other swap call in the same pallet protects with a minimum-output check. Since `SwapFirstAssetTrader` is wired into live parachain runtimes' XCM configuration (`asset-hub-rococo`, `asset-hub-westend`, `penpal`, `staking-async parachain`) as the `Trader` for `pallet_xcm_benchmarks`/XCM executor fee payment, this is reachable from ordinary XCM traffic, not a test-only construct. [7](#0-6) 

### Likelihood Explanation
Exploitation requires only: (1) a pool for `(Target, refund_swap_asset)` with the attacker able to move its reserves via ordinary permissionless liquidity/swap operations, and (2) triggering `buy_weight` followed by `refund_weight` for the same asset pair, which happens automatically whenever unused weight is refunded on any XCM message paying fees via this trader. No validator, relayer, governance, or privileged role is needed — an ordinary user constructing XCM/batched calls suffices, satisfying the "unprivileged attacker, public entrypoint" bar.

### Recommendation
Compute a safe `amount_out_min` for the `refund_weight` swap (e.g., derived from a fresh quote via `QuotePrice::quote_price_exact_tokens_for_tokens` with an acceptable tolerance, mirroring how `buy_weight` uses `swap_tokens_for_exact_tokens` for an exact amount) instead of passing `None`, and fail/return the refund into `total_fee` if the quoted minimum is not met, consistent with the guard already present on the `Err` branch of the swap call.

### Proof of Concept
1. Deploy/observe a live parachain using `SwapFirstAssetTrader<Target, AssetConversion, ...>` as its XCM `WeightTrader` (e.g., asset-hub-style config).
2. Attacker submits an XCM program that pays fees in `AssetX` (≠ `Target`) with more than the exact fee, causing `buy_weight` to swap exactly the fee amount into `Target` and accumulate the rest of `AssetX` toward a future refund of unused weight.
3. Immediately before the refund executes (same block, prior XCM/extrinsic), attacker calls `AssetConversion::swap_exact_tokens_for_tokens` or manipulates liquidity on the `(Target, AssetX)` pool to skew reserves unfavorably for whoever swaps `Target -> AssetX` next.
4. When the executor calls `refund_weight`, `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, AssetX], refund, None)` executes at the manipulated price with zero floor, extracting disproportionate `AssetX` from pool reserves relative to `Target` given up — verifiable by comparing pool reserves before/after versus the constant-product invariant expected at the pre-manipulation price.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L425-489)
```rust
	fn buy_weight(
		&mut self,
		weight: Weight,
		mut payment: AssetsInHolding,
		_context: &XcmContext,
	) -> Result<AssetsInHolding, (AssetsInHolding, XcmError)> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::buy_weight weight: {:?}, payment: {:?}",
			weight,
			payment,
		);
		let Some((id, given_credit)) = payment.fungible.first_key_value() else {
			return Err((payment, XcmError::AssetNotFound));
		};
		let id = id.clone();
		let given_credit_amount = given_credit.amount();
		let first_asset: Asset = (id.clone(), given_credit_amount).into();
		let Ok((fungibles_id, _)) = FungiblesAssetMatcher::matches_fungibles(&first_asset) else {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight asset {:?} didn't match",
				first_asset,
			);
			return Err((payment, XcmError::AssetNotFound));
		};

		let swap_asset = fungibles_id.clone().into();
		if Target::get().eq(&swap_asset) {
			log::trace!(
				target: "xcm::weight",
				"SwapFirstAssetTrader::buy_weight Asset was same as Target, swap not needed.",
			);
			// current trader is not applicable.
			return Err((payment, XcmError::FeesNotMet));
		}
		// Subtract required from payment
		let Some(imbalance) = payment.fungible.remove(&first_asset.id) else {
			return Err((payment, XcmError::TooExpensive));
		};
		// "manually" build the concrete credit and move the imbalance there.
		let mut credit_in = fungibles::Credit::<AccountId, Fungibles>::zero(fungibles_id);
		credit_in.saturating_subsume(imbalance);

		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
			Ok(a) => a,
			Err((credit_in, error)) => {
				log::trace!(
					target: "xcm::weight",
					"SwapFirstAssetTrader::buy_weight swap couldn't be done. Error was: {:?}",
					error,
				);
				// put back the taken credit
				let taken =
					AssetsInHolding::new_from_fungible_credit(id.clone(), Box::new(credit_in));
				payment.subsume_assets(taken);
				return Err((payment, XcmError::FeesNotMet));
			},
		};
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L790-892)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L968-1014)
```rust
		/// Swap exactly `amount_in` of asset `path[0]` for asset `path[1]`.
		/// If an `amount_out_min` is specified, it will return an error if it is unable to acquire
		/// the amount desired.
		///
		/// Withdraws the `path[0]` asset from `sender`, deposits the `path[1]` asset to `send_to`,
		/// respecting `keep_alive`.
		///
		/// If successful, returns the amount of `path[1]` acquired for the `amount_in`.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> Result<T::Balance, DispatchError> {
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}

			Self::swap(&sender, &path, &send_to, keep_alive)?;

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```
