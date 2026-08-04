### Title
`SwapFirstAssetTrader::refund_weight` performs an internal AMM swap with `amount_out_min = None`, allowing XCM fee refunds to be sandwiched down to near zero - ([File: cumulus/primitives/utility/src/lib.rs])

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by AssetHub Rococo/Westend and Penpal XCM configs to let users pay XCM execution fees in a non-native asset by swapping it for the `Target` asset through `pallet-asset-conversion`'s `SwapCredit` interface. In `buy_weight`, the swap correctly uses `swap_tokens_for_exact_tokens`, which has an implicit minimum via the exact `fee` amount. However, in `refund_weight`, the accumulated unused `Target`-asset fee is swapped back into the user's original asset via `SwapCredit::swap_exact_tokens_for_tokens(..., None)` — passing `None` as `amount_out_min`, i.e., no slippage/minimum-output protection at all, exactly mirroring the GMX `openGlpPosition` bug where the minimum-output argument was hardcoded to zero.

### Finding Description
In `cumulus/primitives/utility/src/lib.rs`, `refund_weight` computes the leftover fee credit and swaps it back to the asset the user originally paid with: [1](#0-0) 

```rust
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
```

Compare this with `buy_weight`, which correctly demands an exact target output (`fee`) via `swap_tokens_for_exact_tokens`: [2](#0-1) 

The `SwapCredit::swap_exact_tokens_for_tokens` trait explicitly documents that `amount_out_min` is the caller's only protection against unfavorable pricing: [3](#0-2) 

When `None` is passed, `pallet_asset_conversion`'s implementation performs no minimum-received check at all — it will accept whatever `amount_out` the pool produces, however low: [4](#0-3) 

Because AMM pools used for `SwapFirstAssetTrader` are the same general-purpose `pallet-asset-conversion` pools that anyone can add/remove liquidity from, an attacker who is a block author (or otherwise controls/observes transaction ordering, e.g. via a sandwich around the parachain's XCM-message execution) can transiently drain/skew the `Target` → `refund_swap_asset` pool immediately before the refund swap executes, causing the refund conversion to return a value far below its true worth, and then restore the pool afterward, pocketing the difference. Unlike `buy_weight` (which is bounded by requiring an exact, known-in-advance output) or the top-level `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsics (which enforce a user-supplied non-zero minimum, per `ensure!(amount_out_min > Zero::zero(), ...)`), this internal protocol-initiated refund path has no such backstop.

### Impact Explanation
This directly matches the "Balances… must conserve value and settle exactly once to the rightful beneficiary and amount" and "public underpriced work" pivots: the refunded XCM fee (rightfully belonging to the message sender) can be systematically undervalued and captured by whoever can influence swap ordering around the refund, resulting in real, repeatable loss of user funds embedded in the WeightTrader used by production AssetHub runtimes (Rococo, Westend) and Penpal test/parachain configs. Because `WeightTrader::refund_weight` runs automatically as part of ordinary XCM message execution (no privileged actor required to trigger it — it fires for every message that overestimates its weight), this is a public, unprivileged-attacker-reachable path, not one requiring a malicious validator/relayer/admin.

### Likelihood Explanation
Exploitability depends on the attacker's ability to influence transaction/message ordering around the target pool (e.g., a block author sandwiching the parachain block, or a searcher racing swaps against pending XCM execution) — a standard AMM sandwich pattern, not requiring any privileged network role. Given `pallet-asset-conversion` pools are permissionlessly created/fundable with arbitrary liquidity depth, thin pools used for `Target`⟷`refund_swap_asset` conversion are realistic and would make the attack economically viable with modest capital.

### Recommendation
Compute a minimum acceptable refund amount before swapping — e.g., quote the current price via `QuotePrice::quote_price_exact_tokens_for_tokens` (already available as a trait bound on `SwapFirstAssetTrader`) and apply a slippage tolerance, then pass `Some(min_expected)` instead of `None` to `SwapCredit::swap_exact_tokens_for_tokens` in `refund_weight`. If the swap cannot achieve at least the quoted-minus-tolerance amount, either skip the refund conversion (return the fee in the `Target` asset when possible) or fail closed rather than accepting an arbitrarily bad price.

### Proof of Concept
1. An attacker deploys/observes a shallow `pallet-asset-conversion` pool for `(Target, refund_swap_asset)` used by AssetHub's `SwapFirstAssetTrader`.
2. A user submits an XCM message paying fees in `refund_swap_asset`; `buy_weight` swaps a bounded amount into `Target` for the estimated weight.
3. The message under-consumes weight, triggering `refund_weight`, which swaps the leftover `Target` credit back to `refund_swap_asset` via `swap_exact_tokens_for_tokens(..., None)`.
4. The attacker front-runs this refund swap with a large trade against the same pool (skewing the price against the refund), letting the refund execute at a degraded rate, then back-runs to restore the pool and capture the arbitrage — extracting value that should have gone back to the original XCM fee payer.
5. Repeating this across messages compounds the loss, since every `refund_weight` call using this trader is unprotected. [5](#0-4)

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-475)
```rust
		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L85-97)
```rust
	/// Swap exactly `credit_in` of asset `path[0]` for asset `path[last]`.  If `amount_out_min` is
	/// provided and the swap can't achieve at least this amount, an error is returned.
	///
	/// On a successful swap, the function returns the `credit_out` of `path[last]` obtained from
	/// the `credit_in`. On failure, it returns an `Err` containing the original `credit_in` and the
	/// associated error code.
	///
	/// This operation is expected to be atomic.
	fn swap_exact_tokens_for_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out_min: Option<Self::Balance>,
	) -> Result<Self::Credit, (Self::Credit, DispatchError)>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1002)
```rust
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
```
