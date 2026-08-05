### Title
`SwapFirstAssetTrader::refund_weight` swaps unused XCM fee back to the client asset with no slippage/minimum-output protection - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is the `WeightTrader` used by parachains (e.g. Penpal's `XcmConfig::Trader` in `cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs`) to let users pay XCM execution fees in any asset that has an `AssetConversion` pool with the `Target` (native) asset. When it refunds unused fee, it force-swaps the refund amount back into the client's original asset through the single configured `AssetConversion` instance without specifying a minimum output, exactly mirroring the AuraSpell bug of forcing a swap through one venue with no slippage control. [1](#0-0) 

### Finding Description
In `buy_weight`, the trader correctly bounds the swap by requesting an exact `fee` amount out via `SwapCredit::swap_tokens_for_exact_tokens`, so the user's cost is capped. [2](#0-1) 

However, `refund_weight`, which returns any overpaid/unused portion of the fee to the client asset, calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None`:
```
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
``` [3](#0-2) 

The concrete `SwapCredit` implementation used in real runtimes is `pallet_asset_conversion::Pallet` (see Penpal's `SwapFirstAssetTrader<PenpalNativeCurrency, crate::AssetConversion, ...>`), i.e. a single on-chain AMM pool per asset pair, with no ability to route through multiple pools or an aggregator, and — critically — no floor on the output amount for this particular swap. [4](#0-3) 

`pallet_asset_conversion` pools are permissionlessly creatable by anyone via `create_pool`/`add_liquidity`, and reserves can be arbitrarily thin because there is no minimum-liquidity floor enforced beyond `MinimumBalance` of the underlying assets. An attacker can create (or already control) the pool between `Target` and any asset a victim chooses to pay XCM fees with, seed it with minimal/skewed reserves, and let `refund_weight`'s unmin-boundend swap execute at an arbitrarily bad rate — the difference in value is captured by the pool (and thus the attacker as sole/majority LP) rather than returned to the fee payer.

### Impact Explanation
Because the refund swap has zero slippage protection and is routed exclusively through a single, permissionlessly-creatable `AssetConversion` pool, any XCM message that overpays execution weight in a non-native fungible asset can have its refund value siphoned almost entirely to whoever controls that pool's liquidity. This is a public, unauthenticated value-extraction path against ordinary users' funds during otherwise-normal parachain fee handling — matching the "forced fund loss via forced single-router swap without slippage control" bug class from the source report.

### Likelihood Explanation
Any user (or dApp/wallet) using `SwapFirstAssetTrader` to pay XCM fees in a non-native asset that ends up with a nonzero refund is affected; pool creation and thin-liquidity manipulation require no privileged access, only ordinary signed extrinsics (`create_pool`, `add_liquidity`, `swap_exact_tokens_for_tokens`) available to any account. No malicious validator, collator, or relayer is required — an ordinary account acting as attacker/LP suffices.

### Recommendation
Enforce a minimum-output bound on the refund swap in `refund_weight`, analogous to `buy_weight`'s exact-amount protection — e.g., derive an acceptable `amount_out_min` via `QuotePrice::quote_price_exact_tokens_for_tokens` before swapping, and fail/retain the refund in the `Target` asset if the achievable output falls below that bound, rather than accepting `None`.

### Proof of Concept
1. Attacker creates an `AssetConversion` pool for `(Target, X)` with very small/skewed reserves (permissionless `create_pool` + `add_liquidity`), where `X` is an asset a victim is expected to use for XCM fee payment.
2. Victim submits an XCM program that pays execution fees using asset `X` via `SwapFirstAssetTrader::buy_weight` (swap bounded, works fine) but ends up needing a partial refund of unused weight.
3. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, X], refund, None)` with no minimum output; against the attacker's thin/skewed pool, the swap executes at a heavily unfavorable rate.
4. The victim receives a fraction of the fair refund value in asset `X`; the difference accrues into the pool reserves, which the attacker (as pool creator/LP) can withdraw via `remove_liquidity`.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L340-355)
```rust
/// Provides an implementation of [`WeightTrader`] to charge for weight using the first asset
/// specified in the `payment` argument.
///
/// The asset used to pay for the weight must differ from the `Target` asset and be exchangeable for
/// the same `Target` asset through `SwapCredit`.
///
/// ### Parameters:
/// - `Target`: the asset into which the user's payment will be exchanged using `SwapCredit`.
/// - `SwapCredit`: mechanism used for the exchange of the user's payment asset into the `Target`.
/// - `WeightToFee`: weight to the `Target` asset fee calculator.
/// - `Fungibles`: registry of fungible assets.
/// - `FungiblesAssetMatcher`: utility for mapping [`Asset`] to `Fungibles::AssetId` and
///   `Fungibles::Balance`.
/// - `OnUnbalanced`: handler for the fee payment.
/// - `AccountId`: the account identifier type.
pub struct SwapFirstAssetTrader<
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

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
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
