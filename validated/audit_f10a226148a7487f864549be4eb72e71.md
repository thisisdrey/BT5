Audit Report

## Title
Permissionless XCM weight-fee swap (`SwapFirstAssetTrader::buy_weight`) executes AMM swaps at live pool price with no slippage bound, letting an attacker sandwich the pool to buy chain weight below fair cost - (File: `cumulus/primitives/utility/src/lib.rs`)

## Summary
`SwapFirstAssetTrader::buy_weight` swaps a caller-supplied asset for the chain's native `Target` asset via `pallet_asset_conversion`'s `SwapCredit::swap_tokens_for_exact_tokens`, requesting a fixed `amount_out = fee` with no `amount_in_max`/slippage bound whatsoever, unlike the pallet's own public `swap_tokens_for_exact_tokens` extrinsic. An attacker can permissionlessly skew the pool's reserves via ordinary `add_liquidity`/`swap_exact_tokens_for_tokens` calls immediately before triggering `buy_weight`, so that the AMM-computed `amount_in` for the fixed `fee` output collapses toward zero, letting them consume chain weight while draining the pool's native-asset reserve (funded by LPs) for near-nothing, then reverse the skew to recoup most of their manipulation capital.

## Finding Description
`buy_weight` computes `fee = WeightToFee::weight_to_fee(&weight)` — a fixed amount independent of AMM state — and calls: [1](#0-0) 

`SwapCredit::swap_tokens_for_exact_tokens` is implemented for `pallet_asset_conversion::Pallet<T>` by directly calling `do_swap_credit_tokens_for_exact_tokens(path, credit_in, amount_out)` with **no `amount_in_max` parameter or bound anywhere in the call path**: [2](#0-1) 

This is a structural gap compared to the pallet's own public extrinsic, `swap_tokens_for_exact_tokens`, which requires the caller to supply `amount_in_max` precisely to guard against price manipulation between quote and execution: [3](#0-2) 

Because `amount_in` in the fee-swap path is derived purely from live pool reserves (via the constant-product `get_amount_in` formula) for a *fixed* `amount_out = fee`, and `fee` does not scale with pool depth, an attacker can:
1. Sell the native `Target` asset into the `(swap_asset, Target)` pool via the permissionless `swap_exact_tokens_for_tokens` extrinsic, which decreases the pool's `swap_asset` reserve and increases its `Target` reserve.
2. Immediately submit/execute an XCM message paying weight in `swap_asset`; `buy_weight` computes `amount_in` against the now-skewed reserves, so it costs far less `swap_asset` than fair value to acquire the fixed `fee` amount of `Target` — that `Target` is withdrawn directly from the pool's own reserve (i.e., from LP-deposited funds), not "minted" or paid fairly by the attacker.
3. Reverse the skew with an opposing swap, recovering most of the capital used in step 1 (minus AMM trading fees), while the pool/LPs have permanently lost `fee` worth of `Target` for a near-zero amount of `swap_asset` in return.

The only implicit protection is that `credit_in` — the attacker's own supplied amount — acts as a hard cap (if the pool demands more than `credit_in`, the swap simply fails), but it does not protect against the *opposite* direction: a manipulated-cheap price benefiting the attacker.

## Impact Explanation
This matches the accepted impact category "public underpriced work that degrades block production" combined with a real value-conservation violation: LP-deposited native-asset (`Target`) reserves in the `(swap_asset, Target)` pool are extracted at a manipulated, non-fair exchange rate to pay for XCM execution weight, while the chain's recorded fee accounting (`total_fee`) remains nominally correct (`credit_out == fee`) but that value is sourced from third-party LP funds paid for with a mispriced `amount_in`. Repeated abuse against thin/asset-hub-style pools lets an attacker consume XCM execution weight for a fraction of its economic cost while draining pool liquidity providers, degrading both fair fee-based congestion pricing and pool solvency.

## Likelihood Explanation
No privileged role is required: pool creation, `add_liquidity`, and `swap_exact_tokens_for_tokens` are all permissionless extrinsics on `pallet_asset_conversion`, and the attacker can compose the price-skewing extrinsic with the weight-buying XCM message within the same block, exactly matching the "no tools, permissionless trigger + same-block price shift" pattern. Profitability is directly a function of pool depth: shallower pools require less manipulation capital relative to the fixed `fee` value being extracted, making this most exploitable on newly created or thinly-liquid pools such as those on `asset-hub-rococo`/`asset-hub-westend`/`penpal`-style runtimes that configure `SwapFirstAssetTrader`.

## Recommendation
Add an explicit price/slippage bound to the internal fee-swap path used by `SwapFirstAssetTrader::buy_weight`: either extend `SwapCreditT::swap_tokens_for_exact_tokens` to accept an `amount_in_max` (mirroring the public extrinsic), or have `buy_weight` independently quote an expected `amount_in` via `QuotePrice` before/after the swap and reject (return `XcmError::TooExpensive`) if the realized `amount_in` deviates materially from a TWAP/expected value, rather than unconditionally accepting whatever the instantaneous reserves dictate.

## Proof of Concept
1. Create a `(swap_asset, Target)` pool with modest liquidity via `pallet_asset_conversion::create_pool` + `add_liquidity` (both permissionless).
2. Attacker calls `swap_exact_tokens_for_tokens` selling a sizeable amount of `Target` for `swap_asset`, shrinking the pool's `swap_asset` reserve and growing its `Target` reserve.
3. In the same block, attacker submits an XCM message with `BuyExecution` paying in `swap_asset`; `SwapFirstAssetTrader::buy_weight` (`cumulus/primitives/utility/src/lib.rs:469-489`) calls `SwapCredit::swap_tokens_for_exact_tokens(vec![swap_asset, Target], credit_in, fee)`, which — per `substrate/frame/asset-conversion/src/swap.rs:222-239` — has no `amount_in_max`, so the resulting `amount_in` is computed against the skewed reserves and is far below the pre-manipulation fair price for `fee` worth of `Target`.
4. Attacker swaps the surplus `swap_asset` back for `Target`, recovering most of the capital from step 2, net gaining the difference between fair-value `fee` and the actual manipulated `amount_in` paid, funded by the pool's `Target` reserve.
5. A Rust integration test in `pallet_asset_conversion`'s mock runtime combined with `SwapFirstAssetTrader` can assert that `amount_in` charged for a fixed `fee` output differs materially between an unskewed and an attacker-skewed reserve state, demonstrating the missing bound.

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

**File:** substrate/frame/asset-conversion/src/swap.rs (L222-239)
```rust
	fn swap_tokens_for_exact_tokens(
		path: Vec<Self::AssetKind>,
		credit_in: Self::Credit,
		amount_out: Self::Balance,
	) -> Result<(Self::Credit, Self::Credit), (Self::Credit, DispatchError)> {
		let credit_asset = credit_in.asset();
		with_transaction(|| -> TransactionOutcome<Result<_, DispatchError>> {
			let res = Self::do_swap_credit_tokens_for_exact_tokens(path, credit_in, amount_out);
			match &res {
				Ok(_) => TransactionOutcome::Commit(Ok(res)),
				// wrapping `res` with `Ok`, since our `Err` doesn't satisfy the
				// `From<DispatchError>` bound of the `with_transaction` function.
				Err(_) => TransactionOutcome::Rollback(Ok(res)),
			}
		})
		// should never map an error since `with_transaction` above never returns it.
		.map_err(|_| (Self::Credit::zero(credit_asset), DispatchError::Corruption))?
	}
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
