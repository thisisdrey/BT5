Audit Report

## Title
Unprotected AMM refund swap in `SwapFirstAssetTrader::refund_weight` allows a sandwich attack that drains unspent XCM weight-fee credit - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader` is a `WeightTrader` wired into production XCM configs (asset-hub-rococo, asset-hub-westend, penpal, and the staking-async parachain runtime) that pays XCM execution weight by swapping a user asset for `Target` via `pallet-asset-conversion`. While `buy_weight` bounds its swap with `swap_tokens_for_exact_tokens`, `refund_weight` calls `SwapCredit::swap_exact_tokens_for_tokens(vec![Target::get(), refund_swap_asset], refund, None)` with `amount_out_min = None`, disabling all slippage protection on the refund leg [1](#0-0) .

## Finding Description
`refund_weight` is invoked by the XCM executor whenever a message under-consumes its purchased weight. It extracts `refund_amount` from `self.total_fee` and swaps it back to the asset originally supplied, passing `None` for the minimum output [2](#0-1) . In `pallet_asset_conversion::do_swap_exact_credit_tokens_for_tokens`, the minimum-output check is explicitly conditional: `ensure!(amount_out_min.map_or(true, |a| amount_out >= a), Error::<T>::ProvidedMinimumNotSufficientForSwap)` [3](#0-2) . With `None`, this check is vacuously satisfied for any `amount_out`, so the swap accepts whatever price the pool currently reflects.

This is asymmetric with `buy_weight`, which uses `swap_tokens_for_exact_tokens` to cap `amount_in` for a fixed `fee` output, giving it inherent price protection [4](#0-3) . No equivalent protection exists on the refund path. Since `SwapCredit`'s trait bound already includes `QuotePrice`, a pre-swap quote could have been used to set a floor but is not [5](#0-4) .

An unprivileged attacker who can move the reserves of the relevant `AssetConversion` pool within the same block (via ordinary public swap extrinsics — no privileged role needed) can force the refund swap to execute at an arbitrarily unfavorable price, extracting the difference as MEV via a sandwich (manipulate price → trigger refund → reverse manipulation).

## Impact Explanation
This degrades cost correctness of the XCM fee-refund flow: the refunded asset amount returned to a fee payer can be made arbitrarily small relative to fair value by a party with no special privileges, purely through public AMM interactions timed around ordinary XCM traffic. This matches the "public underpriced work" impact category, since a public, permissionless action (issuing swaps against the same pool) forces mispriced settlement of a chain-controlled balance.

## Likelihood Explanation
The precondition — being able to move an `AssetConversion` pool's reserves and issue XCM messages within the same block — requires no privileged role, malicious node/validator/collator, or infrastructure control; it is achievable by any user with capital and access to public extrinsics/XCM message submission. `SwapFirstAssetTrader` is actively configured in multiple shipping runtimes (`asset-hub-rococo/src/xcm_config.rs`, `asset-hub-westend/src/xcm_config.rs`, `penpal/src/xcm_config.rs`, `staking-async/runtimes/parachain/src/xcm_config.rs`), so any deployment using it with an AMM-backed fee asset of modest liquidity is exposed, and the attack is repeatable per block.

## Recommendation
Do not call `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None` in `refund_weight`. Use the already-bound `QuotePrice` to compute an expected output immediately before the swap and pass `Some(min_out)` (quote minus a configurable tolerance), or fall back to returning the refund in `Target` (skipping the swap) when a safe minimum cannot be established, mirroring how `buy_weight` already treats an unfavorable/failed swap as a hard failure rather than accepting any price.

## Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target=Native, SwapCredit=AssetConversion, ...>` and a limited-liquidity `AssetConversion` pool for `(Native, AssetX)`.
2. Attacker submits an XCM message with an over-provisioned `BuyExecution` in `AssetX`, guaranteeing a `total_fee` in `Native` will later be refunded via `refund_weight`.
3. Immediately before the refund-triggering message executes in the same block, attacker performs a large `swap_exact_tokens_for_tokens(AssetX -> Native)` against the same pool to spike the price of `Native` relative to `AssetX`.
4. `refund_weight` executes `swap_exact_tokens_for_tokens(vec![Native, AssetX], refund, None)` at the manipulated price, yielding far less `AssetX` than fair value [1](#0-0) .
5. Attacker reverses the swap (`Native -> AssetX`) after settlement, realizing the value difference. This is verifiable in a unit/integration test on `SwapFirstAssetTrader::refund_weight` (e.g., extending `cumulus/primitives/utility/src/tests/swap_first.rs`) with a mock pool whose reserves are adjustable between the `buy_weight` and `refund_weight` calls, comparing `refund` credit `.peek()` before/after manipulation.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L386-393)
```rust
impl<
		Target: Get<Fungibles::AssetId>,
		SwapCredit: SwapCreditT<
				AccountId,
				Balance = Fungibles::Balance,
				AssetKind = Fungibles::AssetId,
				Credit = fungibles::Credit<AccountId, Fungibles>,
			> + QuotePrice<AssetKind = Fungibles::AssetId, Balance = Fungibles::Balance>,
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

**File:** cumulus/primitives/utility/src/lib.rs (L529-544)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1097)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
```
