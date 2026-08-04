### Title
Underpriced/imprecise swap-for-fee accounting in `SwapFirstAssetTrader::buy_weight` can leave stranded fee credit and cause quote/charge mismatch during XCM execution - (File: cumulus/primitives/utility/src/lib.rs)

### Summary
The external report's core broken invariant is: a contract performs an internal swap to convert part of one asset into another as part of a combined operation (remove liquidity → swap → re-add liquidity), and the amount computed for the swap leg is imprecise relative to the actual AMM state, leaving dust that is stuck and can be repeatedly triggered by any unprivileged caller. The closest structural analog in this repository is `SwapFirstAssetTrader::buy_weight`/`refund_weight` in `cumulus/primitives/utility/src/lib.rs`, which performs an on-chain AMM swap (via `pallet-asset-conversion`'s `SwapCredit`) to convert a user-supplied fee asset into the `Target` fee asset during XCM `BuyExecution`, using a *quoted* amount from a separate quoting path (`quote_price_tokens_for_exact_tokens`) that can diverge from the actual swap execution price. [1](#0-0) 

### Finding Description
`SwapFirstAssetTrader::buy_weight` takes the first fee asset from `payment`, wraps it in a `Credit`, and calls `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)` to obtain exactly `fee` of the `Target` asset, receiving `credit_change` as the unused portion back [2](#0-1) . This underlying `swap_tokens_for_exact_tokens` call resolves to `pallet_asset_conversion`'s `do_swap_tokens_for_exact_tokens`, whose amount-in for each hop is computed by `get_amount_in`, which rounds *up* via `checked_add(&One::one())` after the division [3](#0-2) . Separately, `quote_weight` (used by callers/wallets/other traders to estimate `necessary_give`) computes the required input using `QuotePrice::quote_price_tokens_for_exact_tokens`, which internally calls the *same* `get_amount_in` but against the reserves *at quote time*, not at execution time [4](#0-3) .

Because `buy_weight` is executed inside the same XCM message that may itself contain multiple `BuyExecution`/asset-transacting instructions altering the pool reserves before the trader runs, and because `refund_weight` performs a second independent swap (`Target → refund_swap_asset`) using `WeightToFee::weight_to_fee(&weight)` against the *current* (potentially different) pool reserves [5](#0-4) , the round-trip buy+refund pair is not guaranteed to be reserve-consistent. The refund swap can revert (dropping the refund and permanently keeping the full `total_fee` credit that gets sent to `OnUnbalanced` on `Drop`), or it can produce a `credit_change` in the original fee asset that gets silently merged back into `payment.subsume_assets(unspent)` even though `unspent`'s value no longer reflects the original conversion rate the user or a preceding trader might have quoted [6](#0-5) .

Unlike `pallet-asset-conversion`'s own `do_add_liquidity`, which strictly bounds slippage with `amount1_min`/`amount2_min` checks [7](#0-6) , `SwapFirstAssetTrader` has **no min/max slippage bound** on the internal swap it performs on behalf of the extrinsic sender — the `fee` (amount_out) is fixed, but the amount of the *user's* asset consumed is whatever the AMM state dictates at execution time, with no `amount_in_max` guard exposed to the caller of the XCM message.

### Impact Explanation
This sits in the public dispatch/XCM-execution path: any account can submit an XCM message using an asset other than the chain's `Target` fee asset to pay fees, triggering `SwapFirstAssetTrader::buy_weight` and `refund_weight` on Asset Hub-style runtimes that wire this trader into their `XcmWeigher`/`Trader` configuration. Because the swap price is determined by live, attacker-influenced pool reserves (an attacker can sandwich their own fee-paying message with swaps against the same pool within the same block, since pool state updates are visible mid-block to a block-builder-adjacent actor, or simply exploit natural reserve drift across the buy/refund pair within one message), the amount debited from the sender's asset for a fixed `Target` fee amount is not bounded, and the refund path can systematically fail to return value, causing:
- Public underpriced/overpriced work: a user's fee-asset balance can be drained beyond the intended fee, or the chain can under-collect relative to the intended weight fee if `credit_change` is miscomputed.
- Value that should be refunded to the sender is instead swept into `OnUnbalanced::on_unbalanced` via `Drop` when the refund swap errors, i.e., stuck/misdirected fee value that never reaches the rightful beneficiary (the sender).

### Likelihood Explanation
Medium: the code path is reachable by any unprivileged account executing an XCM message with `BuyExecution` using a non-Target fee asset, exactly the intended common use-case for this trader. No admin, governance, validator, or malicious-peer assumption is required — only normal AMM price movement (which can be self-induced by the same account performing another swap on the same pool within the same block) is needed to create a mismatch between quote-time and swap-time reserves.

### Recommendation
Add an explicit `amount_in_max` / slippage bound to `SwapFirstAssetTrader::buy_weight`, derived from a `quote_weight`-style pre-check performed atomically with (or immediately preceding, within the same transactional context as) the actual `swap_tokens_for_exact_tokens` call, and reject the swap if the realized amount-in exceeds that bound, returning the untouched asset to `payment` instead of executing an unbounded swap. For `refund_weight`, ensure that a failed refund swap does not silently forfeit the entire `total_fee` to `OnUnbalanced`; instead return the un-refunded `Target`-denominated credit as-is to the holding register, or fall back to returning `Target` asset directly rather than attempting a second best-effort swap with no slippage protection.

### Proof of Concept
Conceptual sequence (no PoC harness available in-index; would need to be built in a Devin session against the Asset Hub runtime test harness that wires `SwapFirstAssetTrader` with `pallet-asset-conversion`):
1. Create a pool `(Target, FeeAsset)` with small reserves.
2. Submit an XCM message that (a) performs a large `swap_exact_tokens_for_tokens` against the same pool moving reserves unfavorably, then (b) in the same message issues `BuyExecution` paying with `FeeAsset`.
3. Observe that `SwapFirstAssetTrader::buy_weight`'s internal `swap_tokens_for_exact_tokens` call consumes a much larger amount of `FeeAsset` than `quote_weight` indicated pre-message, with no `amount_in_max` protecting the sender.
4. Optionally force `refund_weight`'s reverse swap to fail (e.g., by depleting `Target` reserve during message execution) and observe the full `total_fee` credit routed to `OnUnbalanced` instead of partially refunded to the sender. [8](#0-7) [9](#0-8)

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L425-510)
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

		match self.total_fee.subsume(credit_out) {
			Err(credit_out) => {
				// error may occur if `total_fee.asset` differs from `credit_out.asset`, which does
				// not apply in this context.
				defensive!(
					"`total_fee.asset` must be equal to `credit_out.asset`",
					(self.total_fee.asset(), credit_out.asset())
				);
				return Err((payment, XcmError::FeesNotMet));
			},
			_ => (),
		};
		self.last_fee_asset = Some(id.clone());

		if credit_change.peek() != Zero::zero() {
			let unspent = AssetsInHolding::new_from_fungible_credit(id, Box::new(credit_change));
			payment.subsume_assets(unspent);
		}
		Ok(payment)
	}
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

**File:** cumulus/primitives/utility/src/lib.rs (L588-599)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L822-843)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1456-1463)
```rust
			let result = numerator
				.checked_div(&denominator)
				.ok_or(Error::<T>::Overflow)?
				.checked_add(&One::one())
				.ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```
