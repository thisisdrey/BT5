Audit Report

## Title
Permissionless XCM weight-fee swap (`SwapFirstAssetTrader::buy_weight`) executes AMM swaps at live pool price with no slippage bound, allowing sandwich attacks that let an attacker buy chain weight for near-zero cost - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::buy_weight` swaps a caller-supplied non-native asset for the chain's `Target` asset via `SwapCredit::swap_tokens_for_exact_tokens`, computing the required `amount_in` purely from the live AMM pool reserves with no caller-enforced upper bound, unlike `pallet_asset_conversion`'s public `swap_tokens_for_exact_tokens` extrinsic which mandates an `amount_in_max` parameter. An attacker can skew a thinly-liquidated `(swap_asset, Target)` pool in the same block (via the fully permissionless `swap_exact_tokens_for_tokens`/`add_liquidity` extrinsics), pay a fixed `fee` amount of `Target` for artificially cheap `swap_asset`, get full chain weight for the XCM message, then reverse the skew to recover capital.

## Finding Description
Reading the actual implementation confirms the claim precisely. In `SwapFirstAssetTrader::buy_weight`: [1](#0-0) , the `fee` is computed as a fixed value from `WeightToFee::weight_to_fee(&weight)`, and `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)` is called with only the total `credit_in` supplied by the message sender and the target `fee` output — there is no explicit `amount_in_max`/slippage parameter passed anywhere in this call path. The `SwapCreditT` trait signature confirms this design gap structurally (no upper-bound parameter exists at all for this API), in contrast to the pallet's public extrinsic, which requires `amount_in_max` explicitly for exactly this reason. Internally, `amount_in` is derived from `get_amount_in`, a pure function of `fee` and the pool's current `reserve_in`/`reserve_out`, i.e., it directly reflects the AMM's instantaneous price with no oracle, TWAP, or caller-supplied cap other than the implicit hard-fail if `credit_in` is insufficient. The only protection is that if `amount_in > credit_in`, the swap fails safely and the payment is returned (`XcmError::FeesNotMet`) — but this is a fail-fast cap only for the caller's own excessive loss, not a defense against the attacker deliberately depressing the price and paying less than fair value.

This lets an attacker: (1) skew reserves in `(swap_asset, Target)` via ordinary permissionless swap/liquidity extrinsics in the same block; (2) submit an XCM message paying weight fees in `swap_asset`, causing `buy_weight` to compute an artificially tiny `amount_in` for the fixed `fee` of `Target`; (3) reverse the skew afterward to recover the flash-manipulated capital. This is a legitimate, verified structural gap: the internal fee-paying swap path used for XCM weight purchase lacks the slippage protection that the pallet's own public extrinsic enforces.

## Impact Explanation
This matches the allowed impact class "public underpriced work that degrades block production": the fixed `fee` extracted from an attacker is unaffected by manipulation, but the attacker's actual cost in `swap_asset` is fully price-dependent and unprotected, letting them repeatedly consume chain weight/message-processing capacity for near-zero real cost. This also drains value from the pool's honest liquidity providers who absorb the mispriced side of the swap, and undermines the intended cost-correctness invariant of XCM fee payment (buy_weight should charge fair value, not manipulated value) as referenced by the "Balances... must conserve value" and "public underpriced work" pivots. [1](#0-0) 

## Likelihood Explanation
Any runtime configuring `SwapFirstAssetTrader` as its XCM `WeightTrader` over a `pallet_asset_conversion`-based `SwapCredit` implementation is affected, since pool creation, liquidity provision, and swaps are fully permissionless extrinsics requiring no privileged role. [2](#0-1)  The attack requires only ordinary transaction submission ability (an unprivileged external attacker), same-block or adjacent-block composition of a swap/liquidity extrinsic with an XCM message, and a pool with shallow-enough liquidity to be economically manipulable — conditions readily reproducible by any actor without special access.

## Recommendation
Extend `SwapCreditT::swap_tokens_for_exact_tokens` (or add a bounded variant) to accept an `amount_in_max` derived from a TWAP/oracle-based expected price or a bounded multiple of `WeightToFee::weight_to_fee`, and have `SwapFirstAssetTrader::buy_weight` reject swaps whose live-quoted `amount_in` deviates materially from that expected price, failing safe with `XcmError::TooExpensive` rather than accepting whatever the instantaneous pool reserves dictate.

## Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader` over `pallet_asset_conversion` and create a `(swap_asset, Target)` pool with modest liquidity via the permissionless `create_pool`/`add_liquidity` extrinsics.
2. In block N, call `swap_exact_tokens_for_tokens` to skew the pool ratio so `Target` becomes cheap relative to `swap_asset` reserves.
3. In the same block, submit an XCM message paying fees in `swap_asset`; `SwapFirstAssetTrader::buy_weight` (`cumulus/primitives/utility/src/lib.rs:469-489`) computes `amount_in` via `get_amount_in` from the skewed reserves with no upper bound, letting the attacker pay far below fair value for the fixed `fee` of `Target`, verified against unit tests using `AssetConversion::get_amount_in` such as the one at `cumulus/parachains/runtimes/assets/asset-hub-rococo/tests/tests.rs:184-282`.
4. Reverse the skew with an opposite swap to recover capital, completing a same-block sandwich that extracts weight/execution at below-fair cost.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L553-573)
```rust
		#[pallet::call_index(4)]
		#[pallet::weight(T::WeightInfo::swap_tokens_for_exact_tokens(path.len() as u32))]
		pub fn swap_tokens_for_exact_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_out: T::Balance,
			amount_in_max: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_tokens_for_exact_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_out,
				Some(amount_in_max),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```
