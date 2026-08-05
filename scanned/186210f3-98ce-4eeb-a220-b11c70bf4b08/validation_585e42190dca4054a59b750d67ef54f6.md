## Analysis

The reported bug's core broken invariant: an on-chain swap computes its slippage-protection bound (`minTokens`) internally from the *current, manipulable* AMM reserves at execution time, rather than accepting a user-chosen bound — so an attacker can sandwich the swap and force it to execute at a worse price.

The exact local analog exists in `SwapFirstAssetTrader`, the XCM `WeightTrader` used by Asset Hub runtimes to let users pay XCM execution fees in a non-native pool asset via `pallet-asset-conversion`.

### Title
`SwapFirstAssetTrader` performs AMM swaps for XCM fee payment/refund with no caller-supplied slippage bound, enabling sandwich extraction from fee payers - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::buy_weight` and `::refund_weight` automatically execute AMM swaps against `pallet-asset-conversion` pools to convert a user's non-native payment asset into the fee-`Target` asset (and back, on refund). Neither call site lets the caller pass an `amount_in_max`/`amount_out_min` bound: `buy_weight` uses `SwapCredit::swap_tokens_for_exact_tokens`, whose trait signature has no maximum-input parameter at all, and `refund_weight` explicitly passes `None` for `amount_out_min` to `swap_exact_tokens_for_tokens`. Both swaps price off the pool's live reserves at the moment the XCM message is processed within a block, exactly the pattern flagged in the external report.

### Finding Description
`buy_weight` takes the user's entire offered payment as `credit_in` and calls: [1](#0-0) 

The `SwapCredit::swap_tokens_for_exact_tokens` trait method it calls has no `amount_in_max` parameter: [2](#0-1) 

`refund_weight` is worse: it explicitly disables slippage protection by passing `None`: [3](#0-2) 

Both swaps are resolved against `pallet_asset_conversion`'s live reserves via `get_amount_in`/`get_amount_out`, computed from whatever the pool balance is at the moment of execution: [4](#0-3) 

Because block authors/other extrinsics control transaction ordering within a block, an attacker can:
1. Submit a large swap against the same `(payment_asset, Target)` pool immediately before the victim's fee-paying XCM message is included in the block, skewing reserves unfavorably for the victim's swap.
2. Let the victim's `buy_weight`/`refund_weight` execute at the skewed price — the victim's fee-asset is converted at a materially worse rate than the un-manipulated quote, with no on-chain bound to reject it.
3. Reverse the initial swap in a following transaction in the same block, capturing the price difference extracted from the victim (and ultimately from the pool's liquidity providers / the victim's overpaid fee).

Unlike the pallet's own public extrinsics `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`, which take a user-supplied `amount_out_min`/`amount_in_max` (exactly the fix recommended in the external report): [5](#0-4) 

the XCM fee-trader path has no such caller-controlled bound, so existing pallet-level protections do not apply here.

### Impact Explanation
Any Asset Hub runtime configuring `SwapFirstAssetTrader` in its XCM `Trader` (e.g. Asset Hub Westend/Rococo) is affected whenever a user pays XCM execution fees in a pool asset instead of the native token: [6](#0-5) 

This is triggered on ordinary, unprivileged XCM traffic (any incoming message that must pay fees), meaning value can be systematically extracted from fee payers by anyone able to submit transactions in the same block that reference the same AMM pool — no validator/collator/relayer collusion or governance action required.

### Likelihood Explanation
High: the swap executes on every fee payment in a non-native asset, uses only live reserves, and both call sites are proven to lack any minimum-output/maximum-input bound (one structurally, via the trait signature; the other explicitly, via `None`). Front-running/back-running within a single block is a standard, low-cost MEV technique available to any account, requiring no privileged role.

### Recommendation
Add explicit slippage bounds to the `WeightTrader` fee-swap path:
- Extend `SwapCredit::swap_tokens_for_exact_tokens` to accept an optional `amount_in_max`, and have `SwapFirstAssetTrader::buy_weight` compute/enforce a bound (e.g., derived from a fresh `quote_price_tokens_for_exact_tokens` call with a configurable tolerance) before executing the swap.
- In `refund_weight`, pass a real `amount_out_min` (computed via `QuotePrice::quote_price_exact_tokens_for_tokens` with tolerance) instead of `None`.
- Alternatively, bound the maximum price deviation allowed between quote and execution within the same block for these XCM-triggered swaps.

### Proof of Concept
1. Runtime configures `XcmConfig::Trader` to include `SwapFirstAssetTrader<Target, AssetConversion, WeightToFee, ...>` (as in Asset Hub Westend/Rococo).
2. Attacker observes a pending XCM message that will pay fees in asset `X` against pool `(X, Target)`.
3. Attacker submits `swap_exact_tokens_for_tokens(X -> Target, large_amount, ...)` ordered immediately before the victim's message inclusion, shifting the `(X, Target)` reserve ratio against the victim.
4. Victim's message is processed; `SwapFirstAssetTrader::buy_weight` calls `SwapCredit::swap_tokens_for_exact_tokens(vec![X, Target], credit_in, fee)` at the skewed reserves — computed via `get_amount_in` at `substrate/frame/asset-conversion/src/lib.rs:1425-1463` — consuming more of the victim's `X` than a fair-price quote would predict, with no cap to reject the trade.
5. Attacker submits `swap_exact_tokens_for_tokens(Target -> X, ...)` immediately after, restoring the pool and realizing the arbitrage profit extracted from the victim's overpriced fee swap.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-489)
```rust
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

**File:** cumulus/primitives/utility/src/lib.rs (L539-546)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
			Err((refund, _)) => {
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L99-113)
```rust
	/// Swaps a portion of `credit_in` of `path[0]` asset to obtain the desired `amount_out` of
	/// the `path[last]` asset. The provided `credit_in` must be adequate to achieve the target
	/// `amount_out`, or an error will occur.
	///
	/// On success, the function returns a (`credit_out`, `credit_change`) tuple, where `credit_out`
	/// represents the acquired amount of the `path[last]` asset, and `credit_change` is the
	/// remaining portion from the `credit_in`. On failure, an `Err` with the initial `credit_in`
	/// and error code is returned.
	///
	/// This operation is expected to be atomic.
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L525-545)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::swap_exact_tokens_for_tokens(path.len() as u32))]
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L446-470)
```rust
	type Trader = (
		UsingComponents<
			WeightToFee,
			WestendLocation,
			AccountId,
			Balances,
			ResolveTo<StakingPot, Balances>,
		>,
		cumulus_primitives_utility::SwapFirstAssetTrader<
			WestendLocation,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndNonPoolAssets,
			(
				TrustBackedAssetsAsLocation<
					TrustBackedAssetsPalletLocation,
					Balance,
					xcm::v5::Location,
				>,
				ForeignAssetsConvertedConcreteId,
			),
			ResolveAssetTo<StakingPot, crate::NativeAndNonPoolAssets>,
			AccountId,
		>,
	);
```
