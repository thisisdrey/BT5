Audit Report

## Title
Unbounded AMM slippage in XCM weight-fee swap allows sandwich extraction of user assets - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::buy_weight` pays XCM execution fees by swapping the first fungible asset in the incoming holding for the runtime's `Target` fee asset via `SwapCredit::swap_tokens_for_exact_tokens`, a call that fixes only the desired output (`fee`) and accepts no `amount_in_max`. The amount of the user's asset consumed is therefore whatever the live `pallet-asset-conversion` pool computes from its current reserves, letting anyone who moves the pool's reserves immediately before the XCM message executes extract value from the message's asset holding.

## Finding Description
In `buy_weight`, the full `given_credit_amount` of the first fungible asset in the payment holding is taken and swapped via `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)`. [1](#0-0) 
This confirms the exact code path cited in the claim.

The `SwapCreditT` trait used here has no `amount_in_max` parameter, unlike the pallet's public extrinsic and `Swap` trait counterpart, both of which do accept an optional max-input bound. [2](#0-1) [3](#0-2) 

After the swap, any `credit_change` — the unconsumed portion of the asset offered for fees — is returned to the message's holding. [4](#0-3) 
This means the maximum amount the sender can lose is bounded by the total amount of that asset they placed in the message's holding for fee payment, but within that bound, a manipulated pool price directly reduces the `credit_change` returned to the sender — i.e., a real, unbounded-within-holding value transfer to whoever moves the pool reserves around the swap. `pallet-asset-conversion` pools are public, permissionless AMMs reachable via ordinary extrinsics (`swap_exact_tokens_for_tokens`, `add_liquidity`, `remove_liquidity`), so any unprivileged account can shift reserves in the same or a preceding block and reverse the position after the fee swap executes, extracting the difference.

`SwapFirstAssetTrader` is confirmed as a live-code weight trader referenced in `cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs`, `cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs`, `cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs`, and `substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs`, confirming this is wired into production/near-production runtime configuration rather than test-only scaffolding.


## Impact Explanation
This degrades the intended fee-payment invariant that XCM message senders should pay a fair, market-rate fee for a fixed `Target`-asset amount: a pool-reserve manipulator can force the sender's asset holding to absorb an inflated price, capturing the difference as arbitrage profit via a compensating trade on the same public pool. This is a runtime bug compromising intended fee-accounting behavior on live Asset Hub / Penpal / staking-async parachain configurations, matching the "runtime bugs that compromise intended behavior" and "public underpriced work" categories of the impact gate, since value is siphoned from XCM senders through an execution-price primitive that lacks the slippage protection present in every other swap entrypoint in the same pallet.

## Likelihood Explanation
The attack requires only ordinary, permissionless `pallet-asset-conversion` extrinsics that any account can call around the target XCM message's inclusion in the same block — no validator, collator, relayer, or governance privilege is required. Profitability scales with pool depth and non-native fee-asset usage; the missing guard (`amount_in_max`) is structurally absent from `SwapCreditT::swap_tokens_for_exact_tokens`, confirmed by direct comparison against the sibling `Swap` trait and pallet extrinsic that do carry this parameter.

## Recommendation
Add an `amount_in_max` (or equivalent slippage bound) parameter to `SwapCreditT::swap_tokens_for_exact_tokens`, and have `SwapFirstAssetTrader::buy_weight` compute an acceptable bound (e.g., via `QuotePrice::quote_price_tokens_for_exact_tokens` taken immediately before the swap, with a tolerance), failing with `XcmError::TooExpensive` if the live pool price exceeds that bound — mirroring the max-input protections already present in the pallet's public `swap_tokens_for_exact_tokens` extrinsic and `Swap` trait.

## Proof of Concept
1. Attacker observes an incoming XCM message that will pay execution fees in `AssetX` via `SwapFirstAssetTrader` configured with `Target = DOT` on Asset Hub.
2. In the same block, before the message executes, attacker submits `AssetConversion::swap_exact_tokens_for_tokens` (or manipulates liquidity) to spike the `AssetX/DOT` pool price against `AssetX`.
3. The XCM executor invokes `buy_weight`, which calls `SwapCredit::swap_tokens_for_exact_tokens([AssetX, DOT], credit_in, fee)`; with no `amount_in_max`, the call succeeds at the manipulated rate, consuming a disproportionately large amount of `AssetX` from `credit_in` and returning less `credit_change` to the sender's holding.
4. Attacker reverses their pool-manipulating trade, restoring the price and pocketing the extra `AssetX` value extracted from the victim's fee payment as arbitrage profit.

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

**File:** cumulus/primitives/utility/src/lib.rs (L505-509)
```rust
		if credit_change.peek() != Zero::zero() {
			let unspent = AssetsInHolding::new_from_fungible_credit(id, Box::new(credit_change));
			payment.subsume_assets(unspent);
		}
		Ok(payment)
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L62-69)
```rust
	fn swap_tokens_for_exact_tokens(
		sender: AccountId,
		path: Vec<Self::AssetKind>,
		amount_out: Self::Balance,
		amount_in_max: Option<Self::Balance>,
		send_to: AccountId,
		keep_alive: bool,
	) -> Result<Self::Balance, DispatchError>;
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L99-113)
```rust
	/// Swaps a portion of `credit_in` of `path[0]` asset to obtain the desired `amount_out` of
	/// the `path[last]` asset. The provided `credit_in` must be adequate to achieve the target
	/// `amount_out`, or an error will occur.
	///
	/// On success, the function returns a (`credit_out`, `credit_change`) tuple, where `credit_out`
	/// represents the acquired amount of the `path[last]` asset, and `credit_change` is the
	/// remaining portion from the `credit_in`. On failure, an `Err` with the initial `credit_in`
	/// and error code is returned.
	///
	/// This operation is expected to be atomic.
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)>;
```
