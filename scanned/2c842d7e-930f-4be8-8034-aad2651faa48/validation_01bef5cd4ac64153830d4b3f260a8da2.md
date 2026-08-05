## Title
Permissionless XCM weight-fee swap (`SwapFirstAssetTrader`) executes AMM swaps at live pool price with no slippage bound, letting an attacker sandwich the pool to buy chain weight for near-zero cost - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
The external report's core broken invariant is: a permissionless entrypoint executes an unbounded, price-sensitive action (buying an asset "at market") using a live AMM/oracle price with no caller-supplied slippage bound, so anyone can manipulate the price immediately before triggering it and extract value. In `polkadot-sdk`, the closest local analog is `SwapFirstAssetTrader::buy_weight` in `cumulus/primitives/utility/src/lib.rs`, used as the XCM `Trader` on Asset Hub runtimes. It swaps a message-sender-supplied asset for the chain's native/`Target` asset via `pallet_asset_conversion`'s `SwapCredit::swap_tokens_for_exact_tokens`, which - unlike the pallet's own public extrinsic - has **no `amount_in_max`/slippage parameter at all**, deriving `amount_in` purely from whatever the AMM pool reserves say at that instant.

## Finding Description
`SwapFirstAssetTrader::buy_weight` (invoked by anyone who sends an XCM message with `BuyExecution` using a non-native, pool-tradeable asset) does: [1](#0-0) 

It calls `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target::get()], credit_in, fee)`, where `fee = WeightToFee::weight_to_fee(&weight)` is fixed, and `credit_in` is exactly the amount the sender chose to attach to the message. Crucially, the `SwapCreditT` trait this relies on has no upper-bound parameter for the input amount: [2](#0-1) 

Contrast this with the pallet's *own* public extrinsic, which requires the caller to specify `amount_in_max` for exactly this reason: [3](#0-2) 

Because the internal `SwapCredit` path used by `buy_weight` omits this protection, `amount_in` is computed purely from the pool's current reserves via `get_amount_in`, with the caller's `credit_in` acting only as an implicit hard cap (if reserves demand more than `credit_in`, the swap fails safely). This means an attacker can:
1. Deposit liquidity or execute a swap in the `(swap_asset, Target)` pool (via the fully permissionless `pallet_asset_conversion::swap_exact_tokens_for_tokens`/`add_liquidity`) in the same block, skewing reserves so that `get_amount_in` for a fixed `fee` output becomes artificially tiny.
2. In the same block, send/execute an XCM message using `swap_asset` to pay for weight; `buy_weight` swaps only the now-tiny `amount_in` for the full `fee` of native `Target` asset, and the message executes with full chain weight/resources consumed.
3. Reverse the pool skew afterward, restoring price and recovering the temporarily deposited capital (a flashloan-style, same-block sandwich), having paid far less than fair value for the executed block weight.

This is a direct structural analog of the "veCRVlock" bug: a permissionless function transacts against a market price with no price bound, letting a flashloan-style price manipulation extract value from the protocol (here, from other pool LPs / effectively from honest fee payers, and from the chain's weight-pricing invariant) at the expense of correct fee accounting.

## Impact Explanation
This falls under the accepted impact class "public underpriced work that degrades block production or stalls bridge processing." An attacker who can cheaply manipulate a shallow `AssetConversion` pool (no privileged access needed - pool creation/liquidity/swap are all permissionless extrinsics) can pay near-zero effective cost in `swap_asset` for a full unit of chain weight during XCM execution, since:
- The `fee` in native `Target` asset is fixed and unaffected by manipulation, but the actual cost borne by the attacker (`amount_in` of `swap_asset`) is fully price-dependent and unprotected.
- Repeated abuse lets an attacker consume disproportionate weight/message-processing capacity for negligible real cost, degrading fair fee-based congestion pricing and potentially enabling weight-based DoS against the message queue/executor at effectively no cost, while draining value from the pool's liquidity providers who absorb the mispriced side of the swap.

## Likelihood Explanation
Likelihood is high on any asset-hub-style runtime using `SwapFirstAssetTrader` (as configured in `asset-hub-rococo`, `asset-hub-westend`, `penpal`, and `staking-async` parachain runtimes) with thin pools: the attacker needs no privileged role, key, or governance access - only the ability to submit ordinary `pallet_asset_conversion` swap/liquidity extrinsics and to compose them with an XCM message in the same block (or across the block boundary using existing mempool ordering), exactly the "no tools, permissionless trigger + flashloan-style price shift" pattern from the original report.

## Recommendation
Add an explicit slippage/price bound to the internal fee-swap path: extend `SwapCreditT::swap_tokens_for_exact_tokens` (or add a bounded variant) to accept a caller/trader-enforced `amount_in_max` derived from a recent/moving-average price (or simply cap it to a bounded multiple of `WeightToFee::weight_to_fee`), and have `SwapFirstAssetTrader::buy_weight` reject swaps whose live-quoted `amount_in` deviates materially from an oracle/TWAP-based expected price, failing safe (`XcmError::TooExpensive`) rather than accepting whatever the instantaneous reserves dictate.

## Proof of Concept
1. Deploy/observe an Asset Hub-style runtime pool `(swap_asset, NativeTarget)` with modest liquidity, tradeable through the permissionless `pallet_asset_conversion::create_pool`/`add_liquidity`/`swap_exact_tokens_for_tokens` calls.
2. In block N, attacker calls `swap_exact_tokens_for_tokens` to push the pool ratio so that `Target` becomes artificially "cheap" in terms of `swap_asset` reserves (standard AMM price impact from a large one-sided swap).
3. In the same block, attacker submits/executes an XCM message paying weight fees in `swap_asset`; `SwapFirstAssetTrader::buy_weight` (`cumulus/primitives/utility/src/lib.rs:425-509`) calls `SwapCredit::swap_tokens_for_exact_tokens(..., credit_in, fee)` with no `amount_in_max`, so `amount_in` computed from the skewed reserves is far below the pool's normal fair-value price for `fee` worth of `Target`.
4. Attacker reverses the skew with an opposite swap, recovering the temporarily used capital, having paid the fixed `fee` for full weight/message execution at a fraction of its fair cost - the sandwich pattern from the original report reproduced against the chain's own permissionless fee-swap trader instead of an external treasury contract.

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
