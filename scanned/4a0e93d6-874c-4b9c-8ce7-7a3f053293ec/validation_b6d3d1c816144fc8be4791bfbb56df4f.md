### Title
Spot-price oracle from a permissionlessly-created, unbounded-liquidity `pallet-asset-conversion` pool is used to price transaction fees and XCM delivery fees, allowing cheap AMM manipulation to misprice fee payments - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
The Sherlock report describes `BondOracleAdapter::getPool` selecting/trusting a price from an AMM pool without validating the pool's liquidity depth or fee structure, letting an attacker stand up a thin, cheap pool that the protocol then treats as a legitimate price oracle. The same broken invariant — "trust the *current* AMM spot price from a pool that anyone can create with minimal capital, with no liquidity/manipulation-resistance check" — exists in `pallet-asset-conversion` and its consumers `pallet-asset-conversion-tx-payment` (`SwapAssetAdapter`) and the XCM fee-quoting runtime APIs (`query_weight_to_asset_fee`, `query_delivery_fees`).

### Finding Description
`pallet_asset_conversion::Pallet::create_pool` is callable by any signed account for any asset pair [1](#0-0) . The only capital requirement is `PoolSetupFee` plus `MintMinLiquidity` worth of tokens deposited via `add_liquidity` — there is no floor on the pool's economic depth relative to the value it will be used to price, and no cooldown, TWAP, or manipulation-resistance mechanism anywhere in the pallet. `pool_id`/`pool_fee` resolution is deterministic per asset pair (`Ascending`/`WithFirstAsset` locators, [2](#0-1) ), so once an attacker creates the (only) pool for an asset pair, all downstream consumers that need a price for that pair are forced to use it.

The price itself is read directly off the AMM's instantaneous reserves (`get_amount_out`/`get_amount_in`, spot-price math with no time-weighting), and is consumed as an oracle by:

1. `SwapAssetAdapter::withdraw_fee` in `pallet-asset-conversion-tx-payment`, which calls `S::quote_price_tokens_for_exact_tokens` to determine how much of a user-chosen asset is required to cover the native-fee amount, then immediately performs `swap_tokens_for_exact_tokens` against the very same pool state [3](#0-2) .
2. The XCM `XcmPaymentApi::query_weight_to_asset_fee` runtime API, which quotes `assets_common::PoolAdapter::quote_price_tokens_for_exact_tokens` against the live pool to convert a weight-fee into a foreign-asset amount [4](#0-3) , and `query_delivery_fees`, used for cross-chain delivery fee estimation.
3. Runtimes wiring this adapter for real fee collection, e.g. `asset-hub-westend`'s `pallet_asset_conversion_tx_payment::Config` and the kitchensink node's equivalent, both routing to the treasury/staking pot [5](#0-4) [6](#0-5) .

Because pool creation is permissionless and there is no liquidity-depth or price-staleness guard, an attacker can:
- Create a pool for `(NativeAsset, AttackerAsset)` (or for a legitimate but thin/illiquid pair) with the minimum allowed liquidity.
- Within the same block (or via repeated small swaps that the pallet does not rate-limit), swap heavily against the pool to move the spot price to an extreme, call/induce a fee-paying extrinsic (or XCM message requiring `query_weight_to_asset_fee`) that consumes `quote_price_tokens_for_exact_tokens` against the manipulated reserves, and then swap back — extracting value from whichever party (fee payer, or the fee recipient/treasury) is on the losing side of the mispriced swap.
- `SwapAssetAdapter` even performs the withdrawal and the swap as two separate reads/writes of pool state (`quote_price_tokens_for_exact_tokens` then `swap_tokens_for_exact_tokens`) inside one extrinsic, so an attacker controlling other transactions in the same block (or the same signer submitting a sandwich pair) can manipulate reserves between the quote and the executed swap, since nothing pins the executed price to the quoted one beyond the `ensure!(change.peek().is_zero())` sanity check, which only checks *consistency*, not that the price itself is fair.

This directly mirrors the Sherlock finding's root cause: a price-fetching component trusts an AMM pool without validating its economic legitimacy (depth, staleness) before using it for downstream value-bearing decisions (there, bond pricing; here, transaction-fee/XCM-fee pricing and the resulting native-asset settlement).

### Impact Explanation
Mispriced fee payments transfer value between the fee payer and the fee beneficiary (treasury/staking pot) at a rate the attacker controls, rather than a fair market rate. For pools the attacker fully controls (freshly created, low-liquidity pairs), this is a direct value-extraction vector: pay a trivial real fee while draining assets from counterparties who rely on the quoted price (or vice versa, forcing victims to overpay). At the XCM layer, `query_weight_to_asset_fee`/`query_delivery_fees` misquoting can cause either underpriced message delivery (degrading bridge/XCMP throughput economics) or user overcharging, both of which affect protocol economic integrity ("public underpriced work" / broken fee accounting), matching the impact class in the gate (theft/mispriced settlement, no privileged actor needed).

### Likelihood Explanation
High for any asset pair that is not already deeply liquid: `create_pool` requires only a signed origin and the setup fee, `MintMinLiquidity` is a small constant, and nothing in the pallet or the tx-payment adapter checks reserve depth, price staleness, or bounds the acceptable slippage between quote and settlement beyond a zero-change sanity check. No validator, governance, or relayer collusion is required — a single unprivileged account can create the pool and manipulate it.

### Recommendation
- Add a liquidity-depth / minimum-TVL requirement (or an allow-list gated by `AdminOrigin`) before a pool's price can be used as an oracle input for fee payment or XCM fee quoting, decoupling "any pool can exist" from "any pool is trustworthy for pricing."
- Introduce a time-weighted or multi-block price check (or a maximum allowed price deviation from a recent reference) in `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` when used by `SwapAssetAdapter` and the XCM fee APIs.
- In `SwapAssetAdapter::withdraw_fee`, bound the actual swap's realized price to the previously quoted price with an explicit slippage tolerance instead of relying on `swap_tokens_for_exact_tokens`'s internal consistency check alone, and consider re-quoting immediately before withdrawal within the same atomic step to shrink the manipulation window.

### Proof of Concept
Conceptual sequence (no live chain/state access available in this environment to execute it, but derivable directly from the cited code):
1. Attacker calls `AssetConversion::create_pool(origin, Native, AttackerAsset)` and adds the minimum liquidity (`MintMinLiquidity`) via `add_liquidity`, becoming the sole liquidity provider of the only pool for that pair (`do_create_pool`, `substrate/frame/asset-conversion/src/lib.rs:729-788`).
2. Attacker submits a large `swap_exact_tokens_for_tokens(AttackerAsset -> Native)` to push the pool's spot price so that `AttackerAsset` is very cheap relative to `Native`.
3. In the same block, the attacker (or an unwitting victim who chose `AttackerAsset` as their fee asset) submits an extrinsic using `ChargeAssetTxPayment` with `asset_id = AttackerAsset`; `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(AttackerAsset, Native, fee, true)` against the manipulated reserves (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:142-146`), computing a distorted `asset_fee`.
4. Attacker reverses the initial swap (`Native -> AttackerAsset`) to restore the price, pocketing the difference extracted from the fee-paying counterpart (the treasury/staking pot receiving the native-asset side, or the fee payer overpaying `AttackerAsset`), depending on manipulation direction.
5. Equivalent flow applies to `query_weight_to_asset_fee`/`query_delivery_fees` on chains exposing `XcmPaymentApi` (`substrate/frame/staking-async/runtimes/parachain/src/lib.rs:1608-1636`), where a manipulated pool price causes XCM delivery/execution fees to be quoted far below or above their fair value.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L726-751)
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
```

**File:** substrate/frame/asset-conversion/src/types.rs (L102-126)
```rust
/// Pool locator where the `PoolId` is a tuple of `AssetKind`s arranged in ascending order.
pub struct Ascending<AccountId, AssetKind, AccountIdConverter>(
	PhantomData<(AccountId, AssetKind, AccountIdConverter)>,
);
impl<AccountId, AssetKind, AccountIdConverter>
	PoolLocator<AccountId, AssetKind, (AssetKind, AssetKind)>
	for Ascending<AccountId, AssetKind, AccountIdConverter>
where
	AssetKind: Ord + Clone + Encode,
	AccountId: Decode,
	AccountIdConverter: for<'a> TryConvert<&'a (AssetKind, AssetKind), AccountId>,
{
	fn pool_id(asset1: &AssetKind, asset2: &AssetKind) -> Result<(AssetKind, AssetKind), ()> {
		if asset1 > asset2 {
			Ok((asset2.clone(), asset1.clone()))
		} else if asset1 < asset2 {
			Ok((asset1.clone(), asset2.clone()))
		} else {
			Err(())
		}
	}
	fn address(id: &(AssetKind, AssetKind)) -> Result<AccountId, ()> {
		AccountIdConverter::try_convert(id).map_err(|_| ())
	}
}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-176)
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

		let (fee_credit, change) = match S::swap_tokens_for_exact_tokens(
			vec![asset_id, A::get()],
			asset_fee_credit,
			fee,
		) {
			Ok((fee_credit, change)) => (fee_credit, change),
			Err((credit_in, _)) => {
				defensive!("Fee swap should pass for the quoted amount");
				let _ = F::resolve(who, credit_in).defensive_proof("Should resolve the credit");
				return Err(InvalidTransaction::Payment.into());
			},
		};

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);

		Ok((fee_credit, asset_fee))
	}
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
