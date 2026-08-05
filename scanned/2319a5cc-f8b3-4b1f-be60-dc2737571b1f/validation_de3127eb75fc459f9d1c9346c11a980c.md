Based on my investigation, I have enough evidence to produce the analog finding.

### Title
`SwapFirstAssetTrader::refund_weight` disables slippage protection on the fee-refund swap, allowing sandwiched value extraction from XCM fee payers - ([File: cumulus/primitives/utility/src/lib.rs])

### Summary
The external report's core broken invariant is: a party executing a swap on behalf of someone else's funds supplies (or in this case, hard-codes) a permissive "expected output" value, defeating the purpose of slippage protection and letting an adversary extract value through price manipulation around the swap. The local analog is `SwapFirstAssetTrader::refund_weight` in `cumulus/primitives/utility/src/lib.rs`, which performs an AMM swap of the user's fee-refund credit back into the original payment asset while passing `None` as `amount_out_min` to `SwapCredit::swap_exact_tokens_for_tokens`, unconditionally disabling slippage protection for every XCM fee refund that uses this trader.

### Finding Description
`SwapFirstAssetTrader::buy_weight` takes a user's arbitrary asset, swaps it via `pallet_asset_conversion::SwapCredit::swap_tokens_for_exact_tokens` for an exact `Target` fee amount, and accumulates the leftover as `total_fee` [1](#0-0) . When the XCM executor later calls `refund_weight` to return unused weight fees to the user, the trader swaps the excess `Target` asset back into the asset the user originally paid with, but explicitly passes `None` for the slippage bound:

```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
``` [2](#0-1) 

This is a real, exact-input AMM swap through `pallet_asset_conversion`, which does support an `amount_out_min` parameter for exactly this purpose [3](#0-2) . Every other swap entry point in the codebase (`swap_exact_tokens_for_tokens` extrinsic, `SingleAssetExchangeAdapter::exchange_asset`, precompile wrappers) always forwards a caller/protocol-supplied non-`None` minimum [4](#0-3) [5](#0-4) . `refund_weight` is the one path in the repository that intentionally passes `None`, meaning the protocol itself — not just a malicious delegate — removes the "slippage tolerance" check that is present in `do_swap_exact_credit_tokens_for_tokens`:

```rust
ensure!(
    amount_out_min.map_or(true, |a| amount_out >= a),
    Error::<T>::ProvidedMinimumNotSufficientForSwap
);
``` [6](#0-5) 

Because `amount_out_min` is `Option::None`, this `ensure!` always evaluates truthy regardless of pool reserves — functionally identical to the reported bug where a strategist supplies an arbitrary `expectedAssetsOrSharesOut` to make the check tautological. `refund_weight` is invoked automatically as part of ordinary XCM message processing whenever a message overpays for `BuyExecution` weight using a non-`Target` asset and that message is routed through `SwapFirstAssetTrader` — this happens on Asset Hub Westend/Rococo runtimes per `pr_8376.prdoc`, which states `SwapFirstAssetTrader` replaced `TakeFirstAssetTrader` there [7](#0-6) .

### Impact Explanation
Any unprivileged party can submit XCM messages that trade against the same `AssetConversion` pool used for `Target`⇄fee-asset conversion within the same block as a victim's fee-refund swap (e.g. by placing large trades immediately before the block containing the victim message, or by controlling both the buy and sell side of a sandwich around the refund step, which executes deterministically and without any minimum-output guard). Because `refund_weight` accepts whatever `credit_out` the manipulated pool returns, the XCM fee payer's refund can be reduced arbitrarily below its fair value, with the difference captured by the attacker as arbitrage/LP profit. This is unauthorized value extraction from ordinary users' fee refunds during normal chain operation — no validator, collator, relayer, or governance compromise is required.

### Likelihood Explanation
Likelihood is moderate-to-high on any parachain that uses `SwapFirstAssetTrader` with a genuinely liquid, permissionless `pallet_asset_conversion` pool for the `Target`/fee asset pair, since: (1) triggering `refund_weight` only requires overpaying `BuyExecution` fees in a non-native asset, which is routine and attacker-triggerable; (2) manipulating a shallow/thinly-liquid pool is straightforward and requires only ordinary signed swap extrinsics, no special privilege.

### Recommendation
`SwapFirstAssetTrader::refund_weight` should pass a real, computed `amount_out_min` (e.g. derived via `QuotePrice::quote_price_exact_tokens_for_tokens` with a bounded tolerance, or the refund amount itself scaled by a maximum acceptable slippage) instead of `None`, and should fail closed (drop the refund back into `total_fee` / trap the asset) if the quoted minimum is not met, mirroring the protection already enforced by `do_swap_exact_credit_tokens_for_tokens` for every other swap path in the codebase.

### Proof of Concept
1. Deploy a parachain runtime configuring `SwapFirstAssetTrader<Target, AssetConversion, ...>` as (part of) the `WeightTrader`, with a `pallet_asset_conversion` pool for `(Target, ClientAsset)` that has moderate liquidity.
2. Victim sends an XCM message paying `BuyExecution` fees with `ClientAsset`, overpaying by `X` so that `refund_weight` will later swap `X` worth of `Target` back to `ClientAsset` via `SwapCredit::swap_exact_tokens_for_tokens(..., None)` [8](#0-7) .
3. Attacker, in the same block (or immediately prior), submits a large `swap_tokens_for_exact_tokens`/`swap_exact_tokens_for_tokens` extrinsic against the same pool to skew the `Target`→`ClientAsset` price unfavorably.
4. When the victim's XCM message's `refund_weight` executes, the unprotected swap returns far less `ClientAsset` than fair value; attacker reverses their trade afterward to restore the price and pocket the difference as arbitrage profit, at the victim's expense — with no `ProvidedMinimumNotSufficientForSwap`-style error ever raised because `amount_out_min` was `None`.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L465-489)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L527-545)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1096)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
```

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L106-114)
```rust
		// Do the swap.
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
```

**File:** prdoc/stable2506/pr_8376.prdoc (L1-9)
```text
title: 'Remove TakeFirstAssetTrader from AH Westend and Rococo'
doc:
- audience: [Runtime Dev, Runtime User]
  description: |-
    Removed `TakeFirstAssetTrader` from Asset Hub Westend and Rococo. Improved macros, fixed tests.
    This implies asset sufficiency no longer guarantees that weight can also be bought with it.
    `SwapFirstAssetTrader` is used instead which will attempt to swap some of the given asset for the
    required amount of native asset to buy weight. This may or may not succeed depending on whether
    there is a local pool present with enough liquidity to serve the swap.
```
