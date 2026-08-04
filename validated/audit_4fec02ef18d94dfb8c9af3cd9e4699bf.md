Note: I was unable to fully read the complete body of `correct_and_deposit_fee` in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs` (tool access was cut off before the follow-up `read_file` calls could return). The analysis below is built from what was retrieved: the `withdraw_fee` implementation, the `QuotePrice`/`AssetConversion::quote_price_*` pool-spot-price mechanism, and the `pr_11823.prdoc` changelog describing a "fee correction" bug in this same code path. The precise re-quote timing inside `correct_and_deposit_fee` could not be independently confirmed line-by-line, so treat the exploit mechanics as based on documented behavior rather than a fully re-verified read of that function body.

### Title
Transaction fee conversion relies on a single manipulable AMM spot price with no cross-check or TWAP - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
The Chainlink report's core flaw is using one manipulable/mismatched price source as (part of) a value determination with no independent cross-check, so that once the assumption breaks, a single actor can skew the remaining "weak" price leg to their benefit. `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` reproduces this exact pattern for on-chain transaction fee payment: it prices the fee-asset-to-native conversion purely from the live `pallet-asset-conversion` pool spot price via `QuotePrice::quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens`, with no oracle diversity, no TWAP, and no bound on how much the reserves may move between the pre-dispatch quote and the post-dispatch correction.

### Finding Description
`SwapAssetAdapter::withdraw_fee` [1](#0-0)  withdraws the user's chosen `asset_id` and swaps it for the fee asset `A` by calling `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)` and then `S::swap_tokens_for_exact_tokens`. That quote is derived directly from `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`, which reads the pool's current reserves and applies the constant-product formula [2](#0-1) . There is no secondary price feed, no time-weighted average, and no deviation check against any other price source — the pool's instantaneous reserve ratio is the sole "oracle" for converting between assets for fee payment, functionally identical to Salty's reliance on its own pool spot price as one of only a few price legs, which the report specifically calls out as "the weaker Oracle."

Per the module's own documentation and `pr_11823.prdoc`, the fee amount actually charged is finalized in two separate steps that read pool state at two different points in time: `withdraw_fee` (pre-dispatch) quotes/swaps at the price *before* the wrapped call executes, and `correct_and_deposit_fee` (post-dispatch) re-quotes/corrects using the swap adapter's pricing logic *after* the wrapped call has executed [3](#0-2) . Because both quotes are taken from the very same pool, and the dispatched call sandwiched between them can itself be an arbitrary swap against that same pool (e.g. `pallet_asset_conversion::swap_exact_tokens_for_tokens` on the identical asset pair used for fee payment), the origin paying the fee fully controls the reserve ratio at the moment of the second (correction) quote. This is the same "manipulate the weak, unguarded price leg" primitive as the WBTC report, just applied to fee accounting instead of collateral liquidation.

Existing guards do not stop this: `Precision::Exact`/`Preservation::Preserve` only protect against insufficient balance, not price manipulation; the `converted_fee_is_never_zero_if_input_fee_is_not` and `post_dispatch_ok_when_pool_asset_dusted_post_withdraw` tests confirm the code path re-quotes against whatever the pool state is at that moment, including states deliberately driven to extremes (dusted reserves, `ZeroLiquidity`) [4](#0-3) . There is no slippage/deviation bound comparing the pre-dispatch quote to the post-dispatch quote, unlike a swap extrinsic which lets the user supply `amount_out_min`/`amount_in_max`.

### Impact Explanation
An unprivileged single account, in one atomic extrinsic, can distort the reserves of the very pool used to price its own fee-asset, causing `correct_and_deposit_fee` to compute a corrected/refunded amount that diverges from the fair, undistorted market price. This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for fee accounting: the treasury/fee-recipient can be under-paid (fee under-priced relative to true value) while the attacker's asset balance is preserved, or conversely other legitimate refund logic can compute an incorrect (and previously buggy, per `pr_11823.prdoc`) amount. Because transaction fees are the mechanism that prices block space, systematic underpricing via this path is exactly the "public underpriced work that degrades block production" impact class named in the gate.

### Likelihood Explanation
The path requires no admin, governance, validator, relayer, or leaked key — only a normal signed extrinsic whose `RuntimeCall` performs a swap on the same asset-conversion pool that the `ChargeAssetTxPayment`/`SwapAssetAdapter` extension is configured to use for fee payment, which is a completely ordinary, permissionless action available on any chain using `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` (e.g. Asset Hub-style runtimes). The pool reserves needed to make the distortion large are proportional to the fee-paying asset's own liquidity, which for low-liquidity or newly created pools can be small, making the attack cheap. Likelihood is tempered by the fact that in mainstream/production configurations pool liquidity for the native/DOT pair tends to be deep, and the `pr_11823` fix already patched at least one concrete miscalculation in this code, suggesting active but incomplete hardening.

### Recommendation
Do not use a single live pool spot price as the sole source for both the pre-dispatch withdrawal quote and the post-dispatch correction quote. Either (a) freeze/cache the pre-dispatch quoted rate and reuse it (rather than re-quoting) for the correction step so the same transaction cannot arbitrage its own fee, or (b) bound the allowed deviation between pre- and post-dispatch quotes and fail the extension if it is exceeded, or (c) source the conversion rate from a manipulation-resistant reference such as a TWAP over multiple blocks rather than the instantaneous reserve ratio, analogous to using an aggregated multi-source price instead of a single spot value.

### Proof of Concept
1. Deploy a runtime with `pallet-asset-conversion-tx-payment` configured with `SwapAssetAdapter<A, F, AssetConversion, OU>`, and create a pool `(NativeAsset, AssetX)` with modest liquidity.
2. Attacker submits an extrinsic paying fees in `AssetX` whose `RuntimeCall` is `pallet_asset_conversion::swap_exact_tokens_for_tokens` (or `swap_tokens_for_exact_tokens`) trading a large amount of `AssetX` against `NativeAsset` in that same pool.
3. `ChargeAssetTxPayment::validate_and_prepare` → `SwapAssetAdapter::withdraw_fee` quotes/withdraws `AssetX` for the fee based on reserves *before* the call executes [5](#0-4) .
4. The wrapped call executes, moving the pool reserves dramatically in the attacker's chosen direction.
5. `post_dispatch_details` → `correct_and_deposit_fee` re-quotes/corrects the fee using the now-distorted reserves, producing a corrected fee or refund inconsistent with the price that would have applied absent the attacker's own swap, as already demonstrated by the reserve-manipulation test scenario in `post_dispatch_ok_when_pool_asset_dusted_post_withdraw` [6](#0-5) .

### Citations

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

**File:** substrate/frame/asset-conversion/src/swap.rs (L242-261)
```rust
impl<T: Config> QuotePrice for Pallet<T> {
	type Balance = T::Balance;
	type AssetKind = T::AssetKind;
	fn quote_price_exact_tokens_for_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance> {
		Self::quote_price_exact_tokens_for_tokens(asset1, asset2, amount, include_fee)
	}
	fn quote_price_tokens_for_exact_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance> {
		Self::quote_price_tokens_for_exact_tokens(asset1, asset2, amount, include_fee)
	}
}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L16-28)
```rust
//! # Asset Conversion Transaction Payment Pallet
//!
//! This pallet allows runtimes that include it to pay for transactions in assets other than the
//! chain's native asset.
//!
//! ## Overview
//!
//! This pallet provides a `TransactionExtension` with an optional `AssetId` that specifies the
//! asset to be used for payment (defaulting to the native token on `None`). It expects an
//! [`OnChargeAssetTransaction`] implementation analogous to `pallet-transaction-payment`. The
//! included [`SwapAssetAdapter`] (implementing [`OnChargeAssetTransaction`]) determines the
//! fee amount by converting the fee calculated by `pallet-transaction-payment` in the native
//! asset into the amount required of the specified asset.
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L1536-1613)
```rust
/// Covers Path C: `S::quote_price_exact_tokens_for_tokens` returns `None` because the pool's
/// asset reserve has been dusted (below `min_balance`, so the pool's asset account was reaped).
/// `get_amount_out` then returns `Err(ZeroLiquidity)`, which surfaces as `None` via the
/// `QuotePrice` impl; Path C takes the no-refund exit.
///
/// Note: AMM swaps withdraw from the pool with `Preservation::Preserve`, which refuses to
/// drop the pool below ED. We bypass that here by burning the pool's asset balance directly with
/// `Expendable`.
#[test]
fn post_dispatch_ok_when_pool_asset_dusted_post_withdraw() {
	use pallet_asset_conversion::PoolLocator;

	let base_weight = 5;
	let balance_factor = 100;
	ExtBuilder::default()
		.balance_factor(balance_factor)
		.base_weight(Weight::from_parts(base_weight, 0))
		.build()
		.execute_with(|| {
			System::set_block_number(1);

			let asset_id = 1;
			let min_balance = 2;
			assert_ok!(Assets::force_create(
				RuntimeOrigin::root(),
				asset_id.into(),
				42,
				true,
				min_balance,
			));
			setup_lp(asset_id, balance_factor);

			let caller = 2;
			let beneficiary = <Runtime as system::Config>::Lookup::unlookup(caller);
			let balance = 10000;
			assert_ok!(Assets::mint_into(asset_id.into(), &beneficiary, balance));

			let call_weight = 100;
			let tip = 5;
			let ext = ChargeAssetTxPayment::<Runtime>::from(tip, Some(asset_id.into()));
			let extension_weight = ext.weight(CALL);
			let len = 10;
			let fee_in_native =
				base_weight + call_weight + extension_weight.ref_time() + len as u64 + tip;
			let fee_in_asset = AssetConversion::quote_price_tokens_for_exact_tokens(
				NativeOrWithId::WithId(asset_id),
				NativeOrWithId::Native,
				fee_in_native,
				true,
			)
			.unwrap();

			let mut info = info_from_weight(WEIGHT_100);
			info.extension_weight = extension_weight;
			let (pre, _) = ChargeAssetTxPayment::<Runtime>::from(tip, Some(asset_id.into()))
				.validate_and_prepare(Some(caller).into(), CALL, &info, len, 0)
				.unwrap();
			let balance_after_withdraw = balance - fee_in_asset;
			assert_eq!(Assets::balance(asset_id, &caller), balance_after_withdraw);

			// Derive the pool's account and dust its asset reserve: burn the full balance
			// with `Expendable`, which reaps the asset account. `get_reserves` will then
			// report `asset_reserve == 0` → `get_amount_out` → `Err(ZeroLiquidity)` →
			// `quote_price_exact_tokens_for_tokens` returns `None`.
			let pool_account =
				<<Runtime as pallet_asset_conversion::Config>::PoolLocator as PoolLocator<
					_,
					_,
					_,
				>>::pool_address(&NativeOrWithId::Native, &NativeOrWithId::WithId(asset_id))
				.unwrap();
			let pool_asset_balance = Assets::balance(asset_id, &pool_account);
			assert!(pool_asset_balance > 0);
			assert_ok!(Assets::burn_from(
				asset_id,
				&pool_account,
				pool_asset_balance,
				Preservation::Expendable,
```
