Audit Report

## Title
`SwapFirstAssetTrader::refund_weight` executes an unbounded-slippage swap with `amount_out_min = None`, allowing fee refunds to be drained via pool-price manipulation - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader` is a live `WeightTrader` implementation used to let users pay XCM execution fees in a non-`Target` asset by swapping through `pallet_asset_conversion` [1](#0-0) . Its `refund_weight` method swaps the unused fee surplus back into the user's original asset by calling `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None`, i.e. with no minimum-output guard [2](#0-1) . This omits the check that `do_swap_exact_credit_tokens_for_tokens` would otherwise enforce when a `Some(min)` is supplied [3](#0-2) , allowing an AMM-pool-price manipulation to reduce a user's fee refund with no error raised.

## Finding Description
`refund_weight` extracts the surplus `Target`-asset credit and swaps it into `refund_swap_asset` (the asset the user originally paid in) via `swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` [4](#0-3) . In contrast, `buy_weight` uses `swap_tokens_for_exact_tokens`, which is bounded by requesting an exact `fee` output amount [5](#0-4) , and `quote_weight` computes an expected rate via `QuotePrice::quote_price_tokens_for_exact_tokens` [6](#0-5) . `do_swap_exact_credit_tokens_for_tokens` in `pallet_asset_conversion` only enforces `ProvidedMinimumNotSufficientForSwap` when `amount_out_min` is `Some` [7](#0-6) ; passing `None` skips this guard entirely, so any nonzero output — however far below fair value — succeeds. This matches the described root cause exactly as claimed, and the code, line numbers, and control flow in the claim are verified accurate.

## Impact Explanation
This is a genuine value-conservation flaw in a fee-refund code path: an XCM sender's rightful refund can be silently reduced by pool-price manipulation, with the shortfall captured by whoever manipulates the pool. This aligns with the required invariant that "assets ... must conserve value and settle exactly once to the rightful beneficiary and amount." However, the severity is bounded: the amount exposed to manipulation is only the *unused weight refund* — a bounded, typically small residual difference between the estimated and actual weight consumed by a single XCM message, not the full fee or any unbounded pool balance. It does not enable theft of arbitrary funds, unbacked minting, duplicate settlement, or origin escalation; it is a bounded slippage/pricing-integrity issue confined to the refund leg of one specific fee-payment convenience feature.

## Likelihood Explanation
Exploitation requires the attacker to manipulate the same `Target`/`refund_swap_asset` pool's reserves within the same block, immediately before the `refund_weight` swap executes, and optionally reverse the position afterward to capture the difference (a sandwich). This requires favorable transaction/message ordering within a block — the attacker does not need validator, collator, or governance privilege, only ordinary public swap/XCM submissions, and low-fee/thin-liquidity pools make this more feasible. This is a real, reachable, unprivileged-actor scenario, consistent with the sandwich mechanism described (not merely front-run-only, since it requires manipulating and typically reversing the price around the victim's refund).

## Recommendation
Compute a minimum acceptable refund amount before calling `swap_exact_tokens_for_tokens` in `refund_weight` — e.g., via `QuotePrice::quote_price_exact_tokens_for_tokens` — and pass `Some(min_acceptable_amount)`, falling back to retaining the `Target` credit (skipping the swap, letting `Drop` route it via `OnUnbalanced`) if the quote cannot be met, mirroring the guard already used by `SingleAssetExchangeAdapter` [8](#0-7) .

## Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target=DOT, ...>` and a `DOT`/`USDT` pool in `pallet_asset_conversion` (as used in `AssetHubRococo`/`AssetHubWestend`/`Penpal` `xcm_config.rs`).
2. Submit an XCM message with an inflated `BuyExecution` weight limit paying in `USDT`; `buy_weight` swaps `USDT → DOT`, leaving unused `total_fee` in `DOT` to be refunded in `USDT`.
3. Before `refund_weight` executes within the same block, submit a large `USDT → DOT` swap against the same pool to depress the `DOT → USDT` rate.
4. `refund_weight` calls `swap_exact_tokens_for_tokens(vec![DOT, USDT], refund, None)`; since `amount_out_min` is `None`, the swap succeeds despite returning far less `USDT` than fair value, per `do_swap_exact_credit_tokens_for_tokens`'s guard being skipped when `amount_out_min` is `None` [9](#0-8) .
5. Attacker reverses their pool-manipulating trade after the refund executes, capturing the difference; the original sender receives a diminished refund with no error raised.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L386-423)
```rust
impl<
		Target: Get<Fungibles::AssetId>,
		SwapCredit: SwapCreditT<
				AccountId,
				Balance = Fungibles::Balance,
				AssetKind = Fungibles::AssetId,
				Credit = fungibles::Credit<AccountId, Fungibles>,
			> + QuotePrice<AssetKind = Fungibles::AssetId, Balance = Fungibles::Balance>,
		WeightToFee: WeightToFeeT<Balance = Fungibles::Balance>,
		Fungibles: fungibles::Balanced<
			AccountId,
			AssetId: 'static,
			OnDropCredit: 'static,
			OnDropDebt: 'static,
		>,
		FungiblesAssetMatcher: MatchesFungibles<Fungibles::AssetId, Fungibles::Balance>,
		OnUnbalanced: OnUnbalancedT<fungibles::Credit<AccountId, Fungibles>>,
		AccountId,
	> WeightTrader
	for SwapFirstAssetTrader<
		Target,
		SwapCredit,
		WeightToFee,
		Fungibles,
		FungiblesAssetMatcher,
		OnUnbalanced,
		AccountId,
	>
where
	Fungibles::Balance: From<u128> + Into<u128>,
{
	fn new() -> Self {
		Self {
			total_fee: fungibles::Credit::<AccountId, Fungibles>::zero(Target::get()),
			last_fee_asset: None,
			_phantom_data: PhantomData,
		}
	}
```

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

**File:** cumulus/primitives/utility/src/lib.rs (L588-598)
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

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L107-114)
```rust
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
```
