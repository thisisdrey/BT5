I have enough evidence to write up the finding now.

### Title
`SwapFirstAssetTrader::refund_weight` performs an unprotected AMM swap (no slippage floor) when refunding unused XCM execution fees, letting the refunded amount be silently reduced below the fair value - ([File: cumulus/primitives/utility/src/lib.rs])

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by parachain XCM configs (e.g. Penpal's `XcmConfig::Trader`) to let users pay XCM execution fees in a non-native asset by swapping it into the chain's `Target` fee asset through `pallet-asset-conversion`'s `SwapCredit`. When unused weight is refunded back to the sender, `refund_weight` swaps the surplus `Target` asset back into the asset the user originally paid with — but it calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hard-set to `None`, i.e. with **no slippage floor at all**. This mirrors the ERC5095 issue where the code silently applies additional, unbounded slippage that the caller has no way to protect against, except here it's worse: the ERC5095 bug at least hard-coded a 1% haircut, whereas this path accepts *any* non-zero output, however small.

### Finding Description
In `buy_weight`, the trader correctly protects the *forward* swap by requesting an **exact** output amount (`swap_tokens_for_exact_tokens(..., fee)`), which fails if the price moved unfavorably: [1](#0-0) 

But in the refund path, the reverse swap intentionally passes `None` for the minimum acceptable output: [2](#0-1) 

Because `amount_out_min` is `None`, `do_swap_exact_credit_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` check entirely and accepts whatever the constant-product pool returns for the given `credit_in`: [3](#0-2) 

`pallet-asset-conversion` pools are public and permissionless — anyone can call `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` or `add_liquidity`/`remove_liquidity` against the exact `(Target, refund_swap_asset)` pool used here. An attacker who can predict or observe an XCM message that will trigger a large `RefundSurplus`/`refund_weight` call (execution weight is deterministic from the program's instructions and is computed before dispatch, and delivery of the refund happens synchronously within the same block as message execution) can temporarily skew the pool's reserves immediately before the refund swap executes, then restore them afterward, extracting the difference between the fair refund and the degraded refund as profit — a classic sandwich, but enabled here specifically because the protocol-side leg of the trade carries *zero* slippage protection, unlike every other swap entry point in the same pallet (`swap_exact_tokens_for_tokens` extrinsic, `SwapAssetAdapter` fee withdrawal, and even `buy_weight` itself all enforce a bound).

### Impact Explanation
This breaks the "Balances/assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant: the XCM sender is the rightful beneficiary of the *fair* refund value for unused weight, but the code allows the trader to hand back an arbitrarily small amount while the `Target` asset it took stays with the trader and ultimately flows to `OnUnbalanced`/the fee/staking-pot account. The corrupted value is the refund `AssetsInHolding` amount returned from `refund_weight`; it can be pushed toward the pool's dust threshold with no bound, which is a genuine underpriced-refund / public-value-drain bug reachable by any unprivileged account that can send XCM programs and interact with the public AMM pool referenced by `Target`/`refund_swap_asset`.

### Likelihood Explanation
Any parachain using `SwapFirstAssetTrader` (Penpal's `XcmConfig` wires it in directly, and it is a general-purpose primitive intended for reuse by production asset-hub-style runtimes) is affected whenever a non-native asset is used to pay XCM fees and the program ends up with unused weight to refund (extremely common, since `Weigher` estimates are conservative). The attack requires only unprivileged access to the public swap/liquidity extrinsics of the same pool and standard XCM message submission — no validator, collator, relayer, or governance role is needed, and no code path elsewhere in the trader compensates for the missing bound.

### Recommendation
Require a caller-meaningful minimum for the refund swap instead of `None`, e.g. derive it from a quoted price at the start of `buy_weight` (already computed via `QuotePrice` in `quote_weight`) with an explicit, bounded tolerance, and treat a failed/insufficient refund swap as "keep the `Target` credit, refund nothing" (as the existing `Err` branch already does) rather than silently accepting whatever the pool returns.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, AssetConversion, WeightToFee, ..., AccountId>` as (part of) `XcmConfig::Trader`, as done in Penpal: [4](#0-3) 
2. Create a thin `(Target, ClientAsset)` pool and have a user submit an XCM program that pays fees in `ClientAsset` via `PayFees`/`BuyExecution`, overshooting the actual weight consumed so a non-trivial `RefundSurplus` occurs.
3. Immediately before the refund swap executes (same block, e.g. a preceding extrinsic or another message processed earlier in the block), the attacker calls `AssetConversion::swap_exact_tokens_for_tokens` or `remove_liquidity` on the same pool to skew reserves so that swapping `Target -> ClientAsset` yields far less than the fair `refund_amount` would imply.
4. `refund_weight` calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, ClientAsset], refund, None)` — with `None` the below check is skipped and the degraded output is accepted unconditionally: [5](#0-4) 
5. The attacker restores the pool afterward (e.g. via `remove_liquidity`/`add_liquidity` or an offsetting swap), pocketing the delta between the fair refund and the actually-returned, slippage-degraded refund. The victim's XCM sender receives a `RefundSurplus` credit that is measurably less than `WeightToFee::weight_to_fee(&weight)` worth of value, with no error and no recourse, exactly analogous to the ERC5095 case where the user is guaranteed to receive less underlying than requested with no way to bound the loss.

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
