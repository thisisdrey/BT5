### Title
Permissionless creation of skewed `pallet-asset-conversion` pools lets an attacker underprice transaction fees paid via `ChargeAssetTxPayment` - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` prices the fee-asset a signer wants to pay with by calling `quote_price_tokens_for_exact_tokens` against a `pallet-asset-conversion` AMM pool. Pool creation (`create_pool`) and liquidity provisioning (`add_liquidity`) are fully permissionless and use the *current spot reserves* with no minimum-depth, minimum-age, or TWAP protection. This mirrors the Splits report exactly: an attacker can spin up a brand-new pool for an asset they control, skew its reserves to make that asset appear extremely "expensive" relative to the native fee asset, and immediately use that pool as the price oracle to pay a negligible amount of the custom asset for an arbitrarily large native-denominated transaction fee.

### Finding Description
`SwapAssetAdapter::withdraw_fee` computes how much of the user-chosen `asset_id` is required to cover `fee` (in the native/target asset `A`) purely from the live pool reserves: [1](#0-0) 

That quote comes from `AssetConversion::quote_price_tokens_for_exact_tokens`, which reads `get_reserves` directly (no averaging, no staleness check, no liquidity floor beyond the tiny `MintMinLiquidity` constant used only at pool genesis): [2](#0-1) 

Both the pool and the asset it is paired with are attacker-controllable: `create_pool` can be called by any signed account for any two distinct `AssetKind`s (the only cost is a small `PoolSetupFee`), and `add_liquidity` lets that same account set the reserve ratio however they like: [3](#0-2) [4](#0-3) 

Runtimes that enable "pay fees in other assets" wire this adapter directly into the fee-charging extension, e.g. Asset Hub and the kitchensink node: [5](#0-4) [6](#0-5) 

Unlike the Uniswap V3 TWAP in the original report (which forces an attacker to *sustain* an artificial price for the whole TWAP window), this oracle is a pure spot price re-read on every call, so the attack requires no waiting period at all — the pool can be created and skewed in one transaction and exploited in the very next one within the same block.

### Impact Explanation
An attacker who creates a fresh, self-owned asset (via the permissionless `pallet-assets::create`), pairs it with the native/fee asset in a new `pallet-asset-conversion` pool, and seeds it with a lopsided ratio (e.g., a tiny amount of the custom asset against a comparatively large amount of native currency) can then pay transaction fees denominated in that custom asset for a small fraction of their true native-currency cost. This is "public underpriced work that degrades block production": the attacker consumes real block weight/space while paying near-nothing of real economic value, letting them spam transactions cheaply and starve honest fee-payers, which is explicitly in scope under the Impact Gate.

### Likelihood Explanation
This requires only a normal signed account with a modest amount of capital (to set the skew) and no privileged role, validator, collator, or relayer collusion — exactly the class of unprivileged, public-entrypoint issue the task asks to prioritize. It is strictly easier to execute than the original Uniswap V3 report because there is no TWAP window to maintain; the pool's instantaneous state is authoritative at the moment `withdraw_fee`/`can_withdraw_fee` runs.

### Recommendation
Do not use raw spot reserves of arbitrary, permissionlessly created pools as a fee-pricing oracle. Options: require a minimum reserve/liquidity depth and/or pool age before a pool can back `ChargeAssetTxPayment`; restrict `T::AssetId` for `pallet-asset-conversion-tx-payment` to a governance-curated allow-list of assets/pools (similar to the whitelisting fix Splits applied); or use a time-weighted/multi-block average price sourced from `pallet-asset-conversion` rather than an instantaneous quote.

### Proof of Concept
1. Attacker calls `pallet_assets::create` to mint a new asset `X` they fully control (permissionless, deposit only).
2. Attacker calls `AssetConversion::create_pool(native, X)` then `AssetConversion::add_liquidity(native, X, huge_native_amount, 1_unit_of_X, ...)`, establishing a pool where `X` is priced as extremely valuable relative to native.
3. In a subsequent transaction (same or next block), attacker submits any call with `ChargeAssetTxPayment::from(tip, Some(X))`. `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, native, fee, true)`, which — given the skewed reserves — returns a tiny (e.g., 1-unit) amount of `X` sufficient to cover a large native `fee`.
4. Attacker's account is debited a negligible amount of self-issued asset `X` while consuming full transaction weight/fee entitlement, repeatable indefinitely to flood the chain with underpriced work.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-157)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;

		// Withdraw the `asset_id` credit for the swap.
		let asset_fee_credit = F::withdraw(
			asset_id.clone(),
			who,
			asset_fee,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Polite,
		)
		.map_err(|_| InvalidTransaction::Payment)?;
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1571-1603)
```rust
		pub fn quote_price_tokens_for_exact_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output =
				T::Assets::reducible_balance(asset2.clone(), &pool_account, Preserve, Polite);
			if amount > max_output {
				return None;
			}

			if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_in(fee, &amount, &balance1, &balance2).ok()
			} else {
				Self::quote(&amount, &balance2, &balance1).ok()
			}
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1176-1188)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = xcm::v5::Location;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		WestendLocation,
		NativeAndNonPoolAssets,
		AssetConversion,
		ResolveAssetTo<StakingPot, NativeAndNonPoolAssets>,
	>;
	type WeightInfo = weights::pallet_asset_conversion_tx_payment::WeightInfo<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L683-695)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = NativeOrWithId<u32>;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		Native,
		NativeAndAssets,
		AssetConversion,
		ResolveAssetTo<TreasuryAccount, NativeAndAssets>,
	>;
	type WeightInfo = pallet_asset_conversion_tx_payment::weights::SubstrateWeight<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
}
```
