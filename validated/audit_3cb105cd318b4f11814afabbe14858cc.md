### Title
`SwapFirstAssetTrader::refund_weight` swaps unused XCM weight-fee back to the client asset with zero slippage protection - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used to let XCM programs pay execution fees in an asset other than the chain's `Target` fee asset, converting it through `pallet_asset_conversion`'s `SwapCredit` implementation. When the program overpays for weight, `refund_weight` converts the unused portion of `Target` back into the asset the user originally paid with — but it calls `swap_exact_tokens_for_tokens(..., None)`, passing `None` as `amount_out_min`. This is the same broken pattern as the Derby `Vault`: a swap executed with **no externally supplied minimum-out**, so it accepts whatever price the AMM state happens to have at execution time.

### Finding Description
`buy_weight` swaps the client's asset into `Target` to pay for weight [1](#0-0) . If the XCM program is refunded weight (e.g. `RefundSurplus`, or automatic surplus refund at program completion), `refund_weight` swaps the leftover `Target` credit back into the client's original asset: [2](#0-1) 

Unlike `buy_weight`, which bounds the swap by `credit_in`/`fee` (an exact-output swap capped by the actual credit supplied), `refund_weight` performs an **exact-input** swap (`swap_exact_tokens_for_tokens`) of the refund amount with `amount_out_min = None`. `pallet_asset_conversion`'s implementation only enforces a minimum when `Some(_)` is supplied [3](#0-2) ; passing `None` disables that guard entirely, so the swap accepts any non-zero output the pool produces at the moment of execution — including a price depressed by a preceding manipulative swap in the same `Target`/`refund_swap_asset` pool.

This contrasts with every other swap call site found in the same codebase area, all of which correctly forward a caller/quote-derived bound: `ChargeAssetTxPayment`'s `SwapAssetAdapter` always uses an exact-output swap sized to the pre-quoted fee and asserts the change is zero [4](#0-3) ; `SingleAssetExchangeAdapter::exchange_asset` always forwards the XCM-message-supplied `want_amount` as the minimum [5](#0-4) . `refund_weight` is the only call site in the swap-consuming code that deliberately drops slippage protection.

### Impact Explanation
Any XCM program that ends up paying its weight fee in a non-`Target` asset and receives a surplus refund (a very common path — `PayFees`/`BuyExecution` combined with `RefundSurplus`, or automatic refund of unused weight at program completion) is exposed to a sandwich attack on the refund leg. An attacker can, in the same block, execute a swap that moves the `Target`↔`refund_swap_asset` pool price against the victim immediately before the victim's XCM message/extrinsic is processed, let the under-priced refund swap execute, then swap back afterward, extracting the difference. This directly matches the "public underpriced work"/"fund loss" pivot: a public code path (any user who submits an XCM program with `pallet_xcm::execute`/`send`, or has an inbound message routed through this trader) loses value with no way to bound the loss, and the attacker needs no privileged role (no malicious collator/validator/relayer required — an ordinary transaction submitter with normal priority/ordering visibility into the block's pending extrinsics/mempool suffices for the front-run/back-run pair).

### Likelihood Explanation
`SwapFirstAssetTrader` is a general-purpose `WeightTrader` intended for parachain runtimes configured to accept multiple fee assets for XCM execution via `pallet_asset_conversion` pools (referenced from PR docs adding it, `prdoc/1.7.0/pr_1845.prdoc`). Whenever it is wired into a runtime's `Trader` (as, e.g., Penpal's `XcmConfig` does with `SwapFirstAssetTrader` [6](#0-5) ), any user submitting an XCM program that slightly overpays weight in a non-native asset triggers `refund_weight`. Because refunds are a normal, frequent part of XCM execution (weight estimates are usually conservative), the unprotected code path is reachable routinely, not only in edge cases, making exploitation straightforward for anyone able to time a swap around the victim's transaction.

### Recommendation
`refund_weight` should not use `None` for `amount_out_min`. Instead, quote the expected refund amount via `QuotePrice::quote_price_exact_tokens_for_tokens` before the swap (the trait is already a supertrait bound on `SwapCredit` here) and pass that quote (minus an acceptable tolerance, or exactly with a check that the delta is zero, mirroring the exact-in-fee pattern used in `ChargeAssetTxPayment`) as `Some(min_out)`. If the resulting `Err` indicates insufficient output, the refund should be safely rejected (return `None`, keeping the unswapped `Target` credit) rather than silently accepting an unbounded loss.

### Proof of Concept
1. Runtime configures `SwapFirstAssetTrader<Target, AssetConversion, WeightToFee, Fungibles, Matcher, OnUnbalanced, AccountId>` as (part of) `XcmConfig::Trader`, with an `AssetConversion` pool between `Target` (e.g. native) and `AssetX`.
2. Victim submits (or has routed to it) an XCM program that pays weight fees in `AssetX`, intentionally or naturally overpaying (e.g. `BuyExecution` with more than needed, or `PayFees` followed by `RefundSurplus`). `buy_weight` swaps some `AssetX` into `Target` to cover the fee [1](#0-0) .
3. Attacker, in the same block just before the victim's transaction/message is processed, submits a large swap in the `Target`/`AssetX` pool that pushes the `Target → AssetX` price down.
4. Victim's XCM program finishes, `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, AssetX], refund, None)` [7](#0-6)  — since `amount_out_min` is `None`, this succeeds even though the victim receives far less `AssetX` than the pre-attack price implied.
5. Attacker reverses their swap immediately after, restoring the pool price and pocketing the value extracted from the victim's under-priced refund — an unbacked value transfer with no privileged access required, unlike every other analogous swap site in the codebase which enforces a caller/quote-derived minimum.

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

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1002)
```rust
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L159-176)
```rust
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

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L107-130)
```rust
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
				Ok(inner) => inner,
				Err((credit_in, error)) => {
					tracing::debug!(
						target: "xcm::SingleAssetExchangeAdapter::exchange_asset",
						?error,
						"Could not perform the swap"
					);
					// put back the taken credit
					let taken = AssetsInHolding::new_from_fungible_credit(
						give_asset.id.clone(),
						Box::new(credit_in),
					);
					give.subsume_assets(taken);
					return Err(give);
				},
			};
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs (L399-413)
```rust
	type Trader = (
		// Allow native asset to pay the execution fee
		UsingComponents<WeightToFee, PenpalNativeCurrency, AccountId, Balances, ToAuthor<Runtime>>,
		// This trader allows to pay with any assets exchangeable to native asset with
		// [`AssetConversion`].
		cumulus_primitives_utility::SwapFirstAssetTrader<
			PenpalNativeCurrency,
			crate::AssetConversion,
			WeightToFee,
			crate::NativeAndAssets,
			(LocalAssetsConvertedConcreteId, ForeignAssetsConvertedConcreteId),
			ResolveAssetTo<StakingPot, crate::NativeAndAssets>,
			AccountId,
		>,
	);
```
