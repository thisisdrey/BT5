Audit Report

## Title
`SwapFirstAssetTrader::refund_weight` swaps unused XCM fee back to the client asset with no slippage/minimum-output protection - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` [1](#0-0)  passing `None` as `amount_out_min`, in contrast to `buy_weight`, which bounds the swap via an exact-output request through `swap_tokens_for_exact_tokens` [2](#0-1) . This asymmetry leaves the refund leg of the fee-payment flow unprotected against unfavorable execution price in the underlying AMM pool.

## Finding Description
The `SwapCredit` trait's `swap_exact_tokens_for_tokens` explicitly supports an `Option<Balance>` minimum-output bound, which is enforced by `pallet_asset_conversion::Pallet::do_swap_exact_credit_tokens_for_tokens` when `Some` is supplied [3](#0-2) . `refund_weight` deliberately passes `None`, meaning the pallet will not reject the swap regardless of how unfavorable the realized exchange rate is [4](#0-3) . Since `pallet_asset_conversion` pools are permissionlessly created and funded (`create_pool`/`add_liquidity` are public extrinsics with no minimum-liquidity floor beyond `MinimumBalance`), an attacker who controls or seeds a thinly/skewed pool for `(Target, X)` can force the refund swap for any user paying XCM fees in asset `X` to execute at an arbitrarily bad rate, capturing the difference as pool value that the attacker as LP can withdraw. The concrete runtime wiring in Penpal's `XcmConfig::Trader` confirms this pathway is live in production-shaped configuration [5](#0-4) .

## Impact Explanation
This breaks the value-conservation invariant for fee refunds: users who overpay execution weight in a non-native asset and are due a refund can have that refund value siphoned to an attacker-controlled AMM pool with no recourse, since the swap direction (`Target` → client asset) has zero minimum-output enforcement. This is unbacked value extraction from ordinary users during standard fee-refund settlement, aligning with the balances/assets conservation pivot requiring settlement to the rightful beneficiary and amount.

## Likelihood Explanation
Exploitation requires only unprivileged, public extrinsics (`create_pool`, `add_liquidity`, ordinary swaps) available to any signed account, with no validator/collator/relayer collusion or privileged access needed. Any XCM message that pays fees via `SwapFirstAssetTrader` in a non-native, non-`Target` asset and triggers a partial refund is exposed whenever the corresponding pool is thin or attacker-controlled.

## Recommendation
Bound the refund swap with an `amount_out_min` derived from `QuotePrice::quote_price_exact_tokens_for_tokens` (or an equivalent acceptable-rate check) before calling `swap_exact_tokens_for_tokens`, and fall back to retaining the refund in the `Target` asset (or returning `None`) if the achievable output falls short, mirroring the exact-output protection already used in `buy_weight`.

## Proof of Concept
1. Attacker permissionlessly creates a `pallet_asset_conversion` pool for `(Target, X)` and seeds it with minimal/skewed reserves via `create_pool` + `add_liquidity`.
2. Victim submits an XCM program paying fees in asset `X` through `SwapFirstAssetTrader::buy_weight` (bounded, succeeds normally) and ends up with unused weight eligible for refund.
3. `refund_weight` calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, X], refund, None)` [6](#0-5) ; against the thin/skewed pool this executes at a heavily unfavorable rate with no rejection possible.
4. Victim receives a fraction of the fair refund in asset `X`; the shortfall accrues into the pool reserves, withdrawable by the attacker via `remove_liquidity`.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L471-475)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L203-220)
```rust
	fn swap_exact_tokens_for_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out_min: Option<Self::Balance>,
	) -> Result<Self::Credit, (Self::Credit, DispatchError)> {
		let credit_asset = credit_in.asset();
		with_transaction(|| -> TransactionOutcome<Result<_, DispatchError>> {
			let res = Self::do_swap_exact_credit_tokens_for_tokens(path, credit_in, amount_out_min);
			match &res {
				Ok(_) => TransactionOutcome::Commit(Ok(res)),
				// wrapping `res` with `Ok`, since our `Err` doesn't satisfy the
				// `From<DispatchError>` bound of the `with_transaction` function.
				Err(_) => TransactionOutcome::Rollback(Ok(res)),
			}
		})
		// should never map an error since `with_transaction` above never returns it.
		.map_err(|_| (Self::Credit::zero(credit_asset), DispatchError::Corruption))?
	}
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
