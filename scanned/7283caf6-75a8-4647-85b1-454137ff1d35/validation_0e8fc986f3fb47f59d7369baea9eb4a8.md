Found the concrete analog: `SwapFirstAssetTrader::refund_weight` in `cumulus/primitives/utility/src/lib.rs`.

### Title
Unprotected AMM refund swap in `SwapFirstAssetTrader::refund_weight` allows a sandwich attack that drains unspent XCM weight-fee credit - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by the XCM executor to pay for message-execution weight by swapping a user-supplied asset for the chain's `Target` fee asset via `pallet-asset-conversion`'s `SwapCredit`. On `buy_weight` it swaps with an exact-output/bounded-input call (`swap_tokens_for_exact_tokens`), which is safe. On `refund_weight`, however, it converts the unused `Target`-asset fee back to the original asset via `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` — passing `None` for `amount_out_min`, i.e., with **no slippage protection at all**.

### Finding Description
`refund_weight` is invoked automatically by the XCM executor for every message that doesn't fully consume its purchased weight [1](#0-0) . It extracts `refund_amount` of `total_fee` and swaps it back into the asset the user originally paid with: [2](#0-1) 

Compare this to `buy_weight`, which correctly uses a bounded swap (`swap_tokens_for_exact_tokens` with an exact `fee` target, so `amount_in` is capped) [3](#0-2) . The refund path uses `swap_exact_tokens_for_tokens` with `amount_out_min = None`, which in `pallet_asset_conversion` skips the minimum-output check entirely: [4](#0-3) 

`do_swap_exact_credit_tokens_for_tokens` only validates `amount_out_min` "if provided" — with `None` there is no floor on `amount_out`, so the swap succeeds at whatever price the pool currently reflects, however unfavorable.

Because XCM message execution (and therefore `buy_weight`/`refund_weight` calls) is triggered by ordinary, unprivileged XCM traffic (e.g., teleports/reserve-transfers with a `BuyExecution` instruction), an attacker can construct a sequence of XCM messages within the same block that:
1. Executes a large swap against the same `AssetConversion` pool (front-run leg) to move the pool's price of `Target` vs. `refund_swap_asset` unfavorably.
2. Sends the victim message (or their own message with intentionally over-provisioned `BuyExecution` fees) so that `refund_weight` executes the unprotected refund swap at the manipulated price, receiving far less of the refund asset than a fair quote would provide.
3. Executes a back-run leg reversing the initial swap, netting the attacker the value the victim's refund lost.

This is a direct AMM "sandwich" on a refund/withdrawal flow, structurally identical to the reported SNX/sUSD swap-without-slippage bug: an on-chain, block-processing-triggered swap executed with no `amount_out_min`.

### Impact Explanation
Value paid by users for XCM execution weight is refunded through this trader when a message under-consumes its purchased weight. An attacker who can manipulate the relevant `AssetConversion` pool's reserves within the same block (via ordinary swap or liquidity calls, no privileged role required) can cause the refund swap to execute at an arbitrarily bad price, extracting the difference as MEV. This constitutes public underpriced work / unbacked value drain of the XCM fee-refund pool and directly harms honest users' fee refunds without needing any malicious peer, validator, collator, or governance actor — matching the in-scope "theft or unbacked mint/unlock" and "public underpriced work" impact categories.

### Likelihood Explanation
`SwapFirstAssetTrader` is intended for parachains that let users pay XCM fees with non-native assets swapped through an on-chain AMM (its README/PR context is asset-conversion-based fee payment for XCM). Any parachain wiring this trader for a fee asset backed by a `pallet-asset-conversion` pool is exposed. The precondition — being able to move pool reserves and issue transactions within the same block window — is realistic for AMM pools with typical liquidity depth and does not require any privileged or malicious-infrastructure assumption; it is exactly the class of "rare but real" AMM slippage condition the original report describes, just triggered automatically by the XCM weight-refund logic rather than a direct user call.

### Recommendation
Do not call `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None` in `refund_weight`. Instead:
- Quote the expected refund via `QuotePrice` (already a supertrait bound on `SwapCredit` here) immediately before the swap and pass a computed minimum (e.g., quote minus a small configurable tolerance) as `Some(min_out)`.
- Alternatively, skip the swap-back entirely and refund in the `Target` asset (or hold it for later batched conversion) when a safe minimum cannot be established, treating an unfavorable price the same way `buy_weight` already treats swap failure (returning `None`/no refund) rather than accepting any price.

### Proof of Concept
Conceptual sequence (requires a testnet fork or emulated-network test harness, not reproducible purely from static code):
1. Configure a parachain runtime with `SwapFirstAssetTrader<Target=Native, SwapCredit=AssetConversion, ...>` and an `AssetConversion` pool for `(Native, AssetX)` with limited liquidity.
2. Attacker submits an XCM `BuyExecution` message that overpays for weight in `AssetX`, ensuring a large `total_fee` in `Native` will later be refunded.
3. Immediately before the refund-triggering message finalizes in the same block, the attacker (or their own preceding XCM/extrinsic) performs a large `swap_exact_tokens_for_tokens(AssetX -> Native)` against the same pool, spiking the price of `Native` in terms of `AssetX`.
4. The victim's `refund_weight` call executes `swap_exact_tokens_for_tokens(vec![Native, AssetX], refund, None)` at the manipulated price, returning far less `AssetX` than fair value.
5. Attacker reverses their swap (`Native -> AssetX`) after refund settles, realizing profit from steps 3–5 while the victim's refund is diminished — demonstrable by comparing `refund` credit `.peek()` value before/after price manipulation in a unit/integration test built around `SwapFirstAssetTrader::refund_weight` and a mock `AssetConversion` pool with adjustable reserves.

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

**File:** cumulus/primitives/utility/src/lib.rs (L512-533)
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
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-558)
```rust
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
