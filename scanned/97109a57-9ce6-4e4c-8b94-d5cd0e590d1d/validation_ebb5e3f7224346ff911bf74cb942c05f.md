## Analysis

The Connext bug's core invariant break is: **a value-settlement swap is executed against a live AMM spot price with no minimum-output (slippage) bound**, letting a sandwicher extract the difference between fair and manipulated price at the expense of whoever is being settled.

The Polkadot SDK equivalent of that exact pattern exists in `SwapFirstAssetTrader::refund_weight`, used by every Asset Hub / Penpal-style runtime that lets users pay XCM execution fees in a non-native asset via `pallet-asset-conversion`.

### Title
Unprotected spot-price AMM swap in XCM `WeightTrader` refund path enables sandwich-attack value extraction - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::buy_weight` correctly swaps the user's fee asset for the `Target` asset via `swap_tokens_for_exact_tokens`, which enforces an implicit maximum (`amount_in_max = credit_in.peek()`). However, the matching `refund_weight` path swaps the *unused* `Target` credit back into the user's original fee asset using `swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`, i.e. no slippage floor whatsoever. [1](#0-0) 

### Finding Description
`refund_weight` extracts `refund_amount` of the `Target` asset from `self.total_fee` and swaps it for the asset the user originally paid with:

```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) { ... }
``` [2](#0-1) 

`SwapCredit::swap_exact_tokens_for_tokens` maps to `Pallet::do_swap_exact_credit_tokens_for_tokens`, which only checks `amount_out >= amount_out_min` *if* `amount_out_min` is `Some`; when it is `None` the check is skipped entirely, and whatever the pool's current reserves dictate is accepted: [3](#0-2) 

This is the identical broken invariant described in the report: a value-bearing swap is executed strictly at spot price with the only bound being "whatever the AMM currently returns," instead of a caller-chosen minimum. `buy_weight` (the corresponding "pay-in" leg) *does* have an effective bound via `amount_in_max`, but the refund leg was left unprotected, mirroring exactly how `_calculatePortalRepayment` bounded the *shortfall* case but let the swap execute regardless of manipulated price.

`buy_weight`/`refund_weight` are invoked for every XCM message processed on any Asset Hub–style runtime that instantiates this trader (`asset-hub-westend`, `asset-hub-rococo`, `penpal`, `staking-async parachain`), as configured in their `Trader` tuples: [4](#0-3) [5](#0-4) 

Any unprivileged actor can:
1. Submit (or cause to be submitted, e.g. via a reserve/teleport transfer with `BuyExecution`) an XCM program that overpays fees in a non-native, pool-backed asset — triggering `buy_weight` then, on partial weight consumption, `refund_weight`.
2. In the same block, sandwich the AMM pool used for that (Target, fee-asset) pair: front-run to skew reserves right before the message executes, let the uncapped `swap_exact_tokens_for_tokens(..., None)` execute at the skewed price, then back-run to restore price and capture the difference.

Because there is no `amount_out_min`, the refund swap cannot revert or protect against this — the attacker's sandwich trade profits directly from the value that should have gone back to the fee payer (or, if the asset is trapped, to whoever later claims the trapped asset).

### Impact Explanation
This degrades the intended "conserve value, settle exactly once to the rightful beneficiary and amount" invariant for public fee-refund settlement in XCM execution — a live, unprivileged, public-entrypoint path (any XCM message with `BuyExecution` in a pool-backed asset) that silently leaks value to a sandwicher on every refund that occurs during price volatility or deliberate manipulation, with no cap on the loss size beyond the size of `total_fee`/pool depth.

### Likelihood Explanation
Any account can construct an XCM message that pays fees in a pool asset and triggers a refund (e.g., attaching more fee than the message ultimately consumes, which is the normal `BuyExecution`+partial-weight-use case). Manipulating a shallow `pallet-asset-conversion` pool with a same-block sandwich (mint liquidity is public, no privileged actor needed) is a standard, low-cost, off-chain-tooling-only attack, making this readily and repeatedly exploitable, especially against low-liquidity pools set up for niche fee-paying assets.

### Recommendation
Add a caller/config-supplied minimum output (or a bounded max-slippage parameter derived from a pre-swap quote, similarly to how `buy_weight` bounds `amount_in_max`) to the `refund_weight` swap, and fail closed (return the un-swapped `Target` refund back into `total_fee`, as already done on `Err`) rather than silently accepting whatever spot price the AMM offers.

### Proof of Concept
1. Deploy `pallet-asset-conversion` pool `(Target, X)` with modest liquidity on an Asset Hub-style runtime using `SwapFirstAssetTrader`.
2. Submit an XCM message with `BuyExecution` paid in asset `X`, over-provisioning the fee so that actual weight used < weight paid for, guaranteeing a `refund_weight` call.
3. In the same block, before the message is executed: (a) swap a large amount into the pool to skew the `(Target, X)` price unfavorably for the upcoming `Target -> X` refund swap; (b) let the victim message execute, triggering the uncapped `swap_exact_tokens_for_tokens(vec![Target, X], refund, None)`; (c) reverse the initial swap.
4. Compare the attacker's post-sandwich balance vs. pre-sandwich balance — profit equals the slippage forced on the refund, taken directly from the value that should have returned to the fee payer. [6](#0-5)

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L980-1014)
```rust
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
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

			Self::swap(&sender, &path, &send_to, keep_alive)?;

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
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
