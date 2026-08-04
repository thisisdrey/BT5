## Analysis

The Nemeos report's core broken invariant is: **an unprivileged actor can cheaply move a price feed the protocol relies on to size an economic action (a loan draw), because the feed reads a manipulable market state instead of a protected, attested value.**

The closest verifiable local analog in this repository is `pallet-asset-conversion`'s reserve pricing. `Pallet::get_reserves` and the `quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens` view functions treat the **raw account balance held by the pool's derived account** as the AMM reserve: [1](#0-0) 

Nothing in `do_create_pool`/`do_add_liquidity` prevents an unrelated account from transferring tokens directly to the pool's `pool_account` (a deterministically derivable address, `T::PoolLocator::address(&pool_id)`) without minting any LP shares: [2](#0-1) [3](#0-2) 

This is confirmed by a recent fix that made the pallet read the **full** balance (not just the reducible balance) of the pool account when computing reserves/prices — evidence that "whatever balance sits in the pool account" is exactly what feeds the price, regardless of how it got there: [4](#0-3) 

This manipulable spot price is then consumed as an oracle by several **public, unprivileged** code paths, exactly like the external oracle in the Nemeos report:

- Transaction-fee payment in a foreign asset (`SwapAssetAdapter::withdraw_fee` / `can_withdraw_fee`), which quotes `asset_fee` from the live pool reserves before withdrawing/swapping: [5](#0-4) 

- XCM `XcmPaymentApi::query_weight_to_asset_fee` / `query_delivery_fees`, which convert weight-based fees or delivery fees into a foreign asset via the same pool price: [6](#0-5) [7](#0-6) 

- The XCM executor's `SwapFirstAssetTrader::quote_weight`, used inside message execution to price required "give" assets for weight, directly off `QuotePrice::quote_price_tokens_for_exact_tokens`: [8](#0-7) 

### Title
Unbacked donation to `pallet-asset-conversion` pool account corrupts AMM spot price used as a fee oracle - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
`pallet-asset-conversion`'s reserve/price functions (`get_reserves`, `quote_price_exact_tokens_for_tokens`, `quote_price_tokens_for_exact_tokens`) treat the pool account's raw token balance as ground truth. Because pool creation is permissionless (`create_pool`, any signed origin, any asset pair) and the pool account address is deterministically derivable, any unprivileged account can send tokens directly to that address — a pure "donation" that changes the priced reserve ratio without minting LP shares and without any invariant check. This manipulated price is consumed as an oracle by `SwapAssetAdapter` (asset-based transaction fee payment), `XcmPaymentApi::query_weight_to_asset_fee`/`query_delivery_fees`, and the XCM executor's `SwapFirstAssetTrader`/`AssetExchanger` used to price weight and delivery fees for cross-chain messages.

### Finding Description
`get_reserves` reads `Self::get_balance(&pool_account, asset)` for both legs of the pair with no accounting for how those tokens arrived (via `add_liquidity` or a bare transfer): [1](#0-0) 

`do_add_liquidity`/`do_create_pool` never restrict who can hold balance at `pool_account`, and there is no LP-share-based reserve accounting (unlike, e.g., recording reserves in dedicated pallet storage that is only mutated by `add_liquidity`/`remove_liquidity`/`swap`). Consequently an attacker can:
1. Call `create_pool` (or use an existing pool) for `asset1`/`asset2`.
2. Transfer `asset1` or `asset2` directly to the pool's `pool_account` (a plain balance transfer, not `add_liquidity`), skewing `reserve1`/`reserve2` in their favor without receiving any LP tokens (so nothing can be "taken back" through `remove_liquidity`).
3. In a subsequent extrinsic in the same block (their own transaction, ordered by their own nonce — no other party's transaction is needed), invoke a downstream consumer of the manipulated price: `ChargeAssetTxPayment`'s `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee` (to under-pay tx fees in the donated asset), or trigger an XCM `Transact`/send whose weight/delivery fee is priced via `SwapFirstAssetTrader::quote_weight` or `AssetExchanger::quote_exchange_price`.

Because `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` are pure reads of current balances (no TWAP, no minimum-liquidity floor beyond `MintMinLiquidity` which only guards LP-token minting, not price reads), the donation instantly and fully moves the spot price with no cooldown, matching the report's core complaint about a floor-price oracle with "no methods to avoid price manipulation."

### Impact Explanation
Downstream consumers treat this pool price as authoritative for economically consequential decisions:
- `SwapAssetAdapter::withdraw_fee`/`correct_and_deposit_fee` size how much of a foreign asset is pulled from a signed account to cover transaction fees — a manipulated price lets the fee payer under-pay relative to the real native-fee cost, i.e., "public underpriced work" that a normal user can trigger against their own extrinsics (degrading fee collection/economics for chains that accept asset-fee payment).
- `XcmPaymentApi::query_delivery_fees`/`query_weight_to_asset_fee` and `SwapFirstAssetTrader` feed into how much of a given asset is charged to cover XCM execution/delivery. An attacker who thinly capitalizes and then donates to a pool used by these APIs/traders can cause under-priced delivery/execution fees to be accepted by the executor, which is exactly the "public underpriced work that ... stalls bridge/message processing" class called out as an accepted impact.

### Likelihood Explanation
Likelihood is high for the pure griefing/self-serving variant: `create_pool` and plain asset transfers are unprivileged, permissionless operations available to any account with minimal capital. No validator, collator, relayer, or governance actor is required — the attacker only needs to sequence two of their own extrinsics (donate, then consume). The main constraint is capital efficiency: donating funds is a sunk cost unless the attacker can recoup more value than they donate through underpaid fees/exchanges, which is protocol- and pool-liquidity-dependent, so exploitability is highest against thinly-liquidated pools (e.g., ones set up specifically to support fee payment in a lesser-known asset), similar to the wash-trading precondition ("well-known, high volume collections mitigate it") called out in the original report.

### Recommendation
- Do not treat the raw balance of the pool account as the reserve; maintain explicit `reserve1`/`reserve2` counters in `PoolInfo` that are only updated by `add_liquidity`, `remove_liquidity`, and `swap`, and reconcile/ignore any balance surplus (or sweep it as protocol revenue) instead of feeding it into price quotes.
- Add minimum-liquidity/depth guards or TWAP-style smoothing to `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` when used by fee-critical callers (`SwapAssetAdapter`, `XcmPaymentApi`, `SwapFirstAssetTrader`), so a single-block donation cannot move the quoted price used for fee/delivery pricing.
- For `SwapAssetAdapter` specifically, consider re-validating the swap outcome against an independent sanity bound (e.g., compare against a recent historical price) rather than trusting the instantaneous quote.

### Proof of Concept
1. Attacker calls `AssetConversion::create_pool(asset_native, asset_X)` and `add_liquidity` with minimal amounts, or targets an existing thin pool.
2. Attacker computes `pool_account = PoolLocator::address(pool_id)` (public, deterministic) and issues a plain `Assets::transfer`/`Balances::transfer` of `asset_X` directly to `pool_account`, inflating `reserve(asset_X)` relative to `reserve(native)` — no LP tokens are minted, confirmed by `do_add_liquidity` only minting LP tokens for its own liquidity path (`substrate/frame/asset-conversion/src/lib.rs:790-892`), never for bare transfers.
3. In the next extrinsic (same account, next nonce, same block), attacker submits a call with `ChargeAssetTxPayment::from(tip, Some(asset_X))`; `SwapAssetAdapter::withdraw_fee` calls `S::quote_price_tokens_for_exact_tokens(asset_X, native, fee, true)` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-146`), which now returns an artificially low `asset_fee` because of the donated reserve skew, so the attacker pays less `asset_X` than the true native-fee-equivalent cost.
4. Equivalently, the attacker (or anyone relying on `XcmPaymentApi::query_delivery_fees`/`query_weight_to_asset_fee`, `polkadot/xcm/pallet-xcm/src/lib.rs:3230-3325`) receives an under-priced fee quote in `asset_X`, and an XCM message priced with `SwapFirstAssetTrader::quote_weight` (`cumulus/primitives/utility/src/lib.rs:564-600`) is accepted for execution while paying less than its real weight/delivery cost.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L726-788)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L790-856)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
		}
```

**File:** prdoc/pr_12408.prdoc (L1-11)
```text
title: 'fix(asset-conversion): use full balances for pool prices'
doc:
- audience: Runtime Dev
  description: |
    `pallet-asset-conversion` now reads full pool account balances when calculating
    pool prices and liquidity amounts. Previously, these calculations used reducible
    balances, which could understate pool reserves when protected funds or unrelated
    non-sufficient assets were held in the pool account.
crates:
- name: pallet-asset-conversion
  bump: patch
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-146)
```rust
	fn withdraw_fee(
		who: &T::AccountId,
		_call: &T::RuntimeCall,
		_dispatch_info: &DispatchInfoOf<<T>::RuntimeCall>,
		asset_id: Self::AssetId,
		fee: Self::Balance,
		_tip: Self::Balance,
	) -> Result<Self::LiquidityInfo, TransactionValidityError> {
		if asset_id == A::get() {
			// The `asset_id` is the target asset, we do not need to swap.
			let fee_credit = F::withdraw(
				asset_id.clone(),
				who,
				fee,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.map_err(|_| InvalidTransaction::Payment)?;

			return Ok((fee_credit, fee));
		}

		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/lib.rs (L1608-1636)
```rust
		fn query_weight_to_asset_fee(weight: Weight, asset: VersionedAssetId) -> Result<u128, XcmPaymentApiError> {
			let native_asset = xcm_config::WestendLocation::get();
			let fee_in_native = WeightToFee::weight_to_fee(&weight);
			let latest_asset_id: Result<AssetId, ()> = asset.clone().try_into();
			match latest_asset_id {
				Ok(asset_id) if asset_id.0 == native_asset => {
					// for native asset
					Ok(fee_in_native)
				},
				Ok(asset_id) => {
					// Try to get current price of `asset_id` in `native_asset`.
					if let Ok(Some(swapped_in_native)) = assets_common::PoolAdapter::<Runtime>::quote_price_tokens_for_exact_tokens(
							asset_id.0.clone(),
							native_asset,
							fee_in_native,
							true, // We include the fee.
						) {
						Ok(swapped_in_native)
					} else {
						log::trace!(target: "xcm::xcm_runtime_apis", "query_weight_to_asset_fee - unhandled asset_id: {asset_id:?}!");
						Err(XcmPaymentApiError::AssetNotFound)
					}
				},
				Err(_) => {
					log::trace!(target: "xcm::xcm_runtime_apis", "query_weight_to_asset_fee - failed to convert asset: {asset:?}!");
					Err(XcmPaymentApiError::VersionedConversionFailed)
				}
			}
		}
```

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3256-3325)
```rust
	/// Given a `destination` and XCM `message`, return assets to be charged as XCM delivery fees.
	///
	/// Meant to be called by the `XcmPaymentApi`.
	/// It's necessary to specify the asset in which fees are desired.
	///
	/// NOTE: Only use this if delivery fees consist of only 1 asset, else this function will error.
	pub fn query_delivery_fees<AssetExchanger: xcm_executor::traits::AssetExchange>(
		destination: VersionedLocation,
		message: VersionedXcm<()>,
		versioned_asset_id: VersionedAssetId,
	) -> Result<VersionedAssets, XcmPaymentApiError> {
		let result_version = destination.identify_version().max(message.identify_version());

		let destination: Location = destination
			.clone()
			.try_into()
			.map_err(|e| {
				tracing::debug!(target: "xcm::pallet_xcm::query_delivery_fees", ?e, ?destination, "Failed to convert versioned destination");
				XcmPaymentApiError::VersionedConversionFailed
			})?;

		let message: Xcm<()> =
			message.clone().try_into().map_err(|e| {
				tracing::debug!(target: "xcm::pallet_xcm::query_delivery_fees", ?e, ?message, "Failed to convert versioned message");
				XcmPaymentApiError::VersionedConversionFailed
			})?;

		let (_, fees) = validate_send::<T::XcmRouter>(destination.clone(), message.clone()).map_err(|error| {
			tracing::debug!(target: "xcm::pallet_xcm::query_delivery_fees", ?error, ?destination, ?message, "Failed to validate send to destination");
			XcmPaymentApiError::Unroutable
		})?;

		// This helper only works for routers that return 1 and only 1 asset for delivery fees.
		if fees.len() != 1 {
			return Err(XcmPaymentApiError::Unimplemented);
		}

		let fee = fees.get(0).ok_or(XcmPaymentApiError::Unimplemented)?;

		let asset_id = versioned_asset_id.clone().try_into().map_err(|()| {
			tracing::trace!(
				target: "xcm::xcm_runtime_apis::query_delivery_fees",
				"Failed to convert asset id: {versioned_asset_id:?}!"
			);
			XcmPaymentApiError::VersionedConversionFailed
		})?;

		let assets_to_pay = if fee.id == asset_id {
			// If the fee asset is the same as the desired one, just return that.
			fees
		} else {
			// We get the fees in the desired asset.
			AssetExchanger::quote_exchange_price(
				&fees.into(),
				&(asset_id, Fungible(1)).into(),
				true, // Maximal.
			)
			.ok_or(XcmPaymentApiError::AssetNotFound)?
		};

		VersionedAssets::from(assets_to_pay).into_version(result_version).map_err(|e| {
			tracing::trace!(
				target: "xcm::pallet_xcm::query_delivery_fees",
				?e,
				?result_version,
				"Failed to convert fees into desired version"
			);
			XcmPaymentApiError::VersionedConversionFailed
		})
	}
```

**File:** cumulus/primitives/utility/src/lib.rs (L564-600)
```rust
	fn quote_weight(
		&mut self,
		weight: Weight,
		given_id: AssetId,
		_context: &XcmContext,
	) -> Result<Asset, XcmError> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::quote_weight weight: {:?}, given_id: {:?}",
			weight,
			given_id,
		);
		if weight.is_zero() {
			return Err(XcmError::NoDeal);
		}

		let give_matcher: Asset = (given_id.clone(), 1).into();
		let (give_fungibles_id, _) = FungiblesAssetMatcher::matches_fungibles(&give_matcher)
			.map_err(|_| XcmError::AssetNotFound)?;
		let want_fungibles_id = Target::get();
		if give_fungibles_id.eq(&want_fungibles_id.clone().into()) {
			return Err(XcmError::FeesNotMet);
		}

		let want_amount = WeightToFee::weight_to_fee(&weight);
		// The `give` amount required to obtain `want`.
		let necessary_give: u128 = <SwapCredit as QuotePrice>::quote_price_tokens_for_exact_tokens(
			give_fungibles_id,
			want_fungibles_id,
			want_amount,
			true, // Include fee.
		)
		.filter(|amount| *amount > 0u128.into())
		.ok_or(XcmError::FeesNotMet)?
		.into();
		Ok((given_id, necessary_give).into())
	}
```
