Based on my investigation, I found a real analog. Let's find `do_swap_credit_tokens_for_exact_tokens` (the credit-based variant used by `SwapCredit`) — it's the counterpart to `do_swap_tokens_for_exact_tokens`, but critically, the `SwapCredit` trait signature (`substrate/frame/asset-conversion/src/swap.rs:109-113`) has **no `amount_in_max` parameter at all**, unlike the `Swap` trait used by the public extrinsic. This means any consumer of `SwapCredit::swap_tokens_for_exact_tokens` — most notably `cumulus_primitives_utility::SwapFirstAssetTrader::buy_weight` (`cumulus/primitives/utility/src/lib.rs:471-489`) — has no way to cap how much of the input asset it is willing to spend to acquire a fixed output amount.

### Title
Missing input-slippage bound in `SwapFirstAssetTrader::buy_weight` fee swap - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::buy_weight` swaps a user's fee-payment asset for an exact amount of the `Target` fee asset via `SwapCredit::swap_tokens_for_exact_tokens`, which — unlike the public `swap_tokens_for_exact_tokens` extrinsic — has no `amount_in_max`/slippage-cap parameter in its trait signature. This is the direct structural analog of the reported Uniswap V3 `rebalance` issue: an automatic, pool-price-dependent swap executed with no bound on the acceptable execution price.

### Finding Description
`pallet_asset_conversion`'s public dispatchable `swap_tokens_for_exact_tokens` (`substrate/frame/asset-conversion/src/lib.rs:1028-1063`) enforces `amount_in <= amount_in_max` via `do_swap_tokens_for_exact_tokens`, giving callers a slippage bound. However the `SwapCredit` trait used internally by XCM machinery (`substrate/frame/asset-conversion/src/swap.rs:109-113`) exposes:

```rust
fn swap_tokens_for_exact_tokens(
    path: Vec<Self::AssetKind>,
    credit_in: Self::Credit,
    amount_out: Self::Balance,
) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
``` [1](#0-0) 

with no `amount_in_max`. This is called from `SwapFirstAssetTrader::buy_weight`: [2](#0-1) 

The `credit_in` fed to the swap is the entire amount of the fee-payment asset offered in the XCM message's holding register for that asset (`payment.fungible.remove(&first_asset.id)`), and `fee` (the exact desired output) is fixed by `WeightToFee::weight_to_fee(&weight)`. `Self::swap` (the internal implementation) executes purely against whatever the current pool reserves happen to be, with no ceiling on `amount_in` — the only implicit "cap" is the size of `credit_in` itself. If pool reserves are manipulated immediately before this XCM-triggered swap executes in the same block (analogous to the sandwich described in the report), the amount of input asset consumed to obtain the fixed `fee` can be arbitrarily larger than intended, up to the full `credit_in` amount, with no error raised as long as enough `credit_in` remains to cover the inflated price.

### Impact Explanation
This is a public, unprivileged-triggerable path: any account submitting an XCM message that has this `WeightTrader` configured (Asset Hub Rococo/Westend and Penpal all wire `SwapFirstAssetTrader` into `type Trader`, per `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:378-393` and similar) triggers this fee-swap path automatically during XCM execution. Absent an input cap, users can be forced to overpay (lose funds) for a fixed weight-fee when the pool price is temporarily distorted, which falls under "theft or unbacked... loss" / value-conservation violations called out in the impact gate, without requiring a malicious validator, collator, or governance actor — only ordinary same-block transaction ordering around the swap-based pool (a normal AMM sandwich, which the impact gate explicitly treats as in-scope when it degrades bridging/settlement-adjacent value conservation, as opposed to "front-run-only" ideas that rely on privileged relayer/validator assumptions).

### Likelihood Explanation
Likelihood is Medium: it requires an attacker to manipulate the relevant AMM pool's reserves around the same block as the fee-paying XCM message, which is possible for any unprivileged actor holding enough of the pooled assets, similar to the original Uniswap V3 sandwich scenario. It does not require any privileged role.

### Recommendation
Add an explicit `amount_in_max` (or an equivalent execution-price bound) to `SwapCredit::swap_tokens_for_exact_tokens`, threaded through `do_swap_credit_tokens_for_exact_tokens`, and have `SwapFirstAssetTrader::buy_weight` compute/enforce a maximum acceptable `amount_in` (e.g., derived from a recent quote plus a bounded tolerance) before performing the swap, mirroring the protection already present in the public `swap_tokens_for_exact_tokens` extrinsic.

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader` as in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs:378-393`, with a live pool between the fee-payment asset and `Target`.
2. Attacker, in the same block, swaps a large amount into the pool to skew the price against the fee-payment asset (front-run leg).
3. Victim's XCM message executes, triggering `SwapFirstAssetTrader::buy_weight`, which calls `SwapCredit::swap_tokens_for_exact_tokens(path, credit_in, fee)` with the distorted reserves — since there is no `amount_in_max`, the swap succeeds by consuming a much larger portion of `credit_in` than the honest price would require.
4. Attacker swaps back (back-run leg), restoring the pool and capturing the difference, while the victim's remaining `credit_in` (refunded to holding) is smaller than expected — an unbacked fund loss for the victim with no error surfaced to detect the mispriced execution. [3](#0-2)

### Citations

**File:** substrate/frame/asset-conversion/src/swap.rs (L109-113)
```rust
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
```

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
