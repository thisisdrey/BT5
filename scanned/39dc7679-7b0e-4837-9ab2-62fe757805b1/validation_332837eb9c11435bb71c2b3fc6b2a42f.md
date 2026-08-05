Found the analog. In `SwapFirstAssetTrader::refund_weight` (used as an XCM `WeightTrader` for chains that charge XCM execution fees in a non-target asset and swap them via `pallet_asset_conversion`), the refund path swaps the unspent portion of `total_fee` (denominated in `Target`) back into the original fee-payment asset with **no slippage protection**: [1](#0-0) 

### Title
`SwapFirstAssetTrader::refund_weight` swaps XCM fee refunds through the AMM with `amount_out_min = None`, exposing users to unbounded slippage/sandwiching - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` implementation used by parachain runtimes to let XCM message senders pay execution fees in an asset other than the chain's `Target` fee asset, swapping via `pallet_asset_conversion`'s `SwapCredit` trait. In `buy_weight`, the initial swap into `Target` correctly bounds the operation with `swap_tokens_for_exact_tokens` (exact-output, capped by `credit_in`), preventing the payer from being shorted. However, in `refund_weight`, when refunding unused fee back into the original payment asset, the code calls: [2](#0-1) 

passing `None` as `amount_out_min`. This is the direct on-chain analog of the reported AMM issue: the router-facing call sets the minimum-received guard to "no minimum," accepting whatever output the pool state yields at execution time.

### Finding Description
`SwapCredit::swap_exact_tokens_for_tokens` (implemented by `pallet_asset_conversion::Pallet<T>`) explicitly supports an `Option<Balance>` `amount_out_min` used to protect against adverse price movement: [3](#0-2) 

The pallet enforces the check only when the caller supplies `Some(min)`; when `None` is passed, `do_swap_exact_credit_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` guard entirely and accepts whatever `amount_out` the AMM curve returns for the given `credit_in` at the moment of execution: [4](#0-3) 

`SwapFirstAssetTrader::refund_weight` calls this exact function with `None`, so the refund swap can be executed against a pool whose reserves have moved (via other swaps interleaved in the same block, or via the attacker's own preceding calls) between `quote_weight`/`buy_weight` and `refund_weight`, with no floor on the amount the user gets back: [5](#0-4) [6](#0-5) 

Existing guards in the pallet (`ZeroAmount`, `ProvidedMinimumNotSufficientForSwap`, path validation) only fire when the caller supplies a real minimum; they cannot protect a caller who opts out by passing `None`. Nothing downstream re-validates the amount received before it's wrapped back into `AssetsInHolding` and returned to the XCM executor as the refund.

### Impact Explanation
This is a public, unprivileged-triggerable underpricing/value-loss path within XCM execution fee handling, which is in-scope as "public underpriced work that degrades block production" and "permanent user-fund... lock" adjacent behavior (fee refund value loss). An attacker who can influence the relevant AMM pool's reserves in the same block (via ordinary, permissionless swap extrinsics on `pallet_asset_conversion`) can cause the refund leg of `SwapFirstAssetTrader` to convert the user's unused fee credit at an unfavorable rate, extracting value from the fee-payer (a classic sandwich against an unprotected AMM leg), while the runtime accepts the result unconditionally. Because refunds happen automatically inside XCM weight accounting (not as a user-initiated extrinsic the user can bound), the affected party has no way to set their own slippage tolerance for this leg.

### Likelihood Explanation
Medium. It requires no privileged access — any account can submit ordinary swap transactions against the same asset-conversion pool used by the trader to move reserves around the block in which a fee-refund swap occurs, and parachains that configure `SwapFirstAssetTrader` (an XCM executor `WeightTrader`) as part of their `XcmConfig` are directly exposed. The precondition is simply "some XCM message pays fees in a non-Target asset via this trader and has unused weight to refund," which is a normal, frequent execution path, not a rare edge case.

### Recommendation
Do not pass `None` for `amount_out_min` in `refund_weight`. Compute a bound using `QuotePrice::quote_price_exact_tokens_for_tokens` (already used elsewhere in this file for `quote_weight`) immediately before the swap, apply a configurable tolerance (e.g., a `Get<Permill>` slippage parameter), and pass `Some(min_expected)` into `SwapCredit::swap_exact_tokens_for_tokens`. If the swap would violate this floor, keep the credit in `total_fee` (as is already done on `Err`) rather than accepting an unbounded-loss trade.

### Proof of Concept
1. Configure a parachain runtime with `SwapFirstAssetTrader<Target, pallet_asset_conversion::Pallet<Runtime>, WeightToFee, ..>` as an XCM `WeightTrader`, backed by a live `Target`/`OtherAsset` pool in `pallet_asset_conversion`.
2. Submit an XCM message that pays execution fees in `OtherAsset` with more than the actually-consumed weight, so `buy_weight` swaps `OtherAsset -> Target` and a nonzero `weight` remains to be refunded via `refund_weight`.
3. Immediately before/around the block containing this XCM message's execution, submit ordinary `pallet_asset_conversion::swap_exact_tokens_for_tokens` calls that shift the `Target`/`OtherAsset` pool reserves unfavorably (e.g., dump `Target` into the pool to depress its price relative to `OtherAsset`).
4. Observe that `refund_weight`'s call `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` at `cumulus/primitives/utility/src/lib.rs:540-544` executes and returns a smaller-than-expected amount of `OtherAsset`, since `amount_out_min = None` disables the `ProvidedMinimumNotSufficientForSwap` check in `do_swap_exact_credit_tokens_for_tokens` (`substrate/frame/asset-conversion/src/lib.rs:1075-1097`), confirming the refunded value to the original fee-payer is silently reduced with no on-chain floor.

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

**File:** cumulus/primitives/utility/src/lib.rs (L512-562)
```rust
	fn refund_weight(&mut self, weight: Weight, _context: &XcmContext) -> Option<AssetsInHolding> {
		log::trace!(
			target: "xcm::weight",
			"SwapFirstAssetTrader::refund_weight weight: {:?}, self.total_fee: {:?}",
			weight,
			self.total_fee,
		);
		if weight.is_zero() || self.total_fee.peek().is_zero() {
			// noting to refund.
			return None;
		}
		let refund_asset = if let Some(asset) = &self.last_fee_asset {
			// create an initial zero refund in the asset used in the last `buy_weight`.
			(asset.clone(), Fungible(0)).into()
		} else {
			return None;
		};
		let refund_amount = WeightToFee::weight_to_fee(&weight);
		if refund_amount >= self.total_fee.peek() {
			// not enough was paid to refund the `weight`.
			return None;
		}

		let refund_swap_asset = FungiblesAssetMatcher::matches_fungibles(&refund_asset)
			.map(|(a, _)| a.into())
			.ok()?;

		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
			Err((refund, _)) => {
				// return an attempted refund back to the `total_fee`.
				let _ = self.total_fee.subsume(refund).map_err(|refund| {
					// error may occur if `total_fee.asset` differs from `refund.asset`, which does
					// not apply in this context.
					defensive!(
						"`total_fee.asset` must be equal to `refund.asset`",
						(self.total_fee.asset(), refund.asset())
					);
				});
				return None;
			},
		};

		let refund = AssetsInHolding::new_from_fungible_credit(refund_asset.id, Box::new(refund));
		Some(refund)
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L989-1002)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1075-1097)
```rust
		pub(crate) fn do_swap_exact_credit_tokens_for_tokens(
			path: Vec<T::AssetKind>,
			credit_in: CreditOf<T>,
			amount_out_min: Option<T::Balance>,
		) -> Result<CreditOf<T>, (CreditOf<T>, DispatchError)> {
			let amount_in = credit_in.peek();
			let inspect_path = |credit_asset| {
				ensure!(
					path.first().map_or(false, |a| *a == credit_asset),
					Error::<T>::InvalidPath
				);
				ensure!(!amount_in.is_zero(), Error::<T>::ZeroAmount);
				ensure!(amount_out_min.map_or(true, |a| !a.is_zero()), Error::<T>::ZeroAmount);

				Self::validate_swap_path(&path)?;
				let path = Self::balance_path_from_amount_in(amount_in, path)?;

				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
```
