Based on my investigation, the strongest local analog to the reported hardcoded-slippage issue lives in `SwapFirstAssetTrader::refund_weight` in `cumulus-primitives-utility`.

### Title
`SwapFirstAssetTrader::refund_weight` performs an on-chain DEX swap with no minimum-output bound - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used by several production XCM configs (e.g. `cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs`, `substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs`) to let users pay XCM execution fees in any asset that has a pool with the native/target asset, via `pallet_asset_conversion`. When refunding unused weight, `refund_weight` swaps the leftover `Target` fee credit back into the asset the user originally paid with, but calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None` [1](#0-0) , i.e. exactly the same "accept whatever the pool returns" pattern flagged in the external report.

### Finding Description
`buy_weight` correctly bounds the swap it performs to acquire fee tokens: it calls `swap_tokens_for_exact_tokens` for an exact `fee` output, which inherently limits the input spent [2](#0-1) . However, `refund_weight`, which returns the *unused* portion of the fee to the payer in their originally-supplied asset, executes:
```
SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,   // <- no amount_out_min
)
``` [3](#0-2) 

`pallet_asset_conversion::do_swap_exact_credit_tokens_for_tokens` treats `None` as "no minimum required" and will accept any non-zero output the pool computes for the given reserves at execution time [4](#0-3) . Unlike `quote_weight`, which does compute an expected price via `QuotePrice::quote_price_tokens_for_exact_tokens` before the trade [5](#0-4) , that computed value is never carried into `refund_weight`'s actual swap as a floor — the refund path independently re-swaps with zero minimum protection, so any adverse pool-state change (regular pool activity, other swaps executed earlier in the same block by the block author/collator, or ordinary AMM price movement) between when the fee was collected and when the refund executes can cause the payer to receive an arbitrarily small refund for a given amount of `Target` asset credit.

### Impact Explanation
The refunded asset is user (fee-payer) funds being converted back through a public, permissionlessly-tradable AMM pool with no floor. Because `pallet_asset_conversion` pools are ordinary public liquidity pools reachable by any account via `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics, pool reserves at execution time are not guaranteed to reflect the reserves used implicitly at fee-collection time, and the code accepts whatever output results, however small. This is a genuine unbounded-slippage / value-loss path for XCM fee payers on any runtime wiring `SwapFirstAssetTrader` as a `Trader` (Penpal test-runtime, staking-async parachain runtime, and any downstream runtime copying this configuration pattern).

### Likelihood Explanation
Low-to-medium: it requires the attacker (or ordinary market activity) to move the pool price between the implicit fee-charge and the refund swap within transaction/XCM execution, which typically happens within the same block. This is analogous to, but distinct from, a pure "front-run only" issue since it does not require a malicious relayer, validator, or collator — any account can submit pool-manipulating extrinsics that land in the same block via normal transaction submission, and refunds happen automatically on every fee-bearing XCM program using this trader with change to refund.

### Recommendation
Thread the amount computed by `quote_weight` (or a freshly computed `quote_price_exact_tokens_for_tokens` value) through as an actual `amount_out_min` in the `refund_weight` swap instead of passing `None`, ensuring the refund swap reverts (and the refund is dropped/held rather than executed at a bad rate) if the realized output falls below the expected value within a defined tolerance.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target, AssetConversion, ...>` as an XCM `Trader` (as done in `penpal/src/xcm_config.rs` or the staking-async parachain runtime).
2. A user submits an XCM program that pays weight fees in asset `X` (not `Target`), causing `buy_weight` to swap `X -> Target` for the exact fee amount, leaving some unused weight to be refunded later in execution.
3. Before the executor reaches `refund_weight` (e.g., via another instruction/dispatch within the same XCM/block that trades against the `X/Target` pool, or simply due to ordinary pool activity from other users in the same block), the `X/Target` pool reserves are shifted so that `Target -> X` now yields far less `X` per unit `Target`.
4. `refund_weight` executes `swap_exact_tokens_for_tokens([Target, X], refund_credit, None)`, which succeeds at the degraded rate because there is no `amount_out_min` floor, and the fee payer receives a much smaller refund than expected in asset `X`, with no error, no event, and no way to recover the difference.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L469-476)
```rust
		let fee = WeightToFee::weight_to_fee(&weight);
		// swap the user's asset for the `Target` asset.
		let (credit_out, credit_change) = match SwapCredit::swap_tokens_for_exact_tokens(
			vec![swap_asset, Target::get()],
			credit_in,
			fee,
		) {
			Ok(a) => a,
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1080-1096)
```rust
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
```
