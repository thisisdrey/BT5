### Title
`SwapFirstAssetTrader::refund_weight` performs an unprotected AMM swap (no `amount_out_min`) when converting unused XCM execution fee back to the original fee asset - ([File: cumulus/primitives/utility/src/lib.rs])

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used in live Asset Hub / Penpal XCM configs to let users pay XCM execution fees in any pool-swappable asset, converting it to the chain's `Target` fee asset via `pallet-asset-conversion`'s `SwapCredit`. The `buy_weight` path correctly guards the swap with a minimum (`fee` amount is passed as the exact `amount_out`, i.e. `swap_tokens_for_exact_tokens`). However `refund_weight`, which swaps the unspent portion of `Target` fee back into the user's original asset, calls `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None`, i.e. no minimum-output/slippage protection at all — the same broken invariant flagged in the Badger `_harvest` report (unprotected AMM swap that can be sandwiched for MEV extraction), but here it sits inside the core XCM fee-refund path of a Substrate-based parachain runtime rather than an off-chain-vault contract.

### Finding Description
`SwapFirstAssetTrader::buy_weight` withdraws the user's non-target asset and swaps it for exactly `fee` amount of `Target` using `SwapCredit::swap_tokens_for_exact_tokens`, which is `amount_out`-exact and therefore bounded. [1](#0-0) 

At the end of message execution, `refund_weight` computes the unused portion of the collected `Target` fee and swaps it back into the asset the user originally paid with, calling:
```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) { ... }
``` [2](#0-1) 

`None` is passed for `amount_out_min`, so `pallet_asset_conversion`'s `do_swap_exact_credit_tokens_for_tokens` skips the `ProvidedMinimumNotSufficientForSwap` check entirely (that check is only executed `if let Some(amount_out_min) = amount_out_min`). [3](#0-2) 

The pool used for this swap (`pallet_asset_conversion` AMM pools on Asset Hub) is a fully public, unprivileged component: any account can add/remove liquidity or perform swaps against the same pool in the immediately preceding/following block(s) or even within the same block via other XCM messages/extrinsics, shifting the reserve ratio. Because the refund swap carries no floor, a party who moves the pool price against the `Target -> refund_swap_asset` direction just before this refund executes (then reverses it after) can force the refund to settle at an arbitrarily bad rate, extracting the difference from the AMM pool's liquidity providers via a sandwich, exactly as described in the external report’s core broken invariant ("no minimum output guard on an AMM swap enables value extraction"). This does not require any relayer, collator, validator, or admin — only a normal account performing ordinary, permissionless swap/liquidity extrinsics against the same public pool that `SwapFirstAssetTrader` depends on, which is why this passes the "unprivileged attacker, public entrypoint" bar.

### Impact Explanation
This degrades the value users receive back from XCM fee overpayment refunds and lets any unprivileged party siphon value out of the public conversion pool that backs the fee-trading logic on a live parachain (Asset Hub / Penpal / staking-async runtime configs all wire up `SwapFirstAssetTrader`). While each individual refund is typically bounded by the small "unused weight" amount, the pattern is a systemic underpriced/unprotected public operation embedded in block-level message execution — repeated extraction across blocks can meaningfully drain LP value with no guard, and there is no atomic-settlement safeguard preventing the state (`total_fee`, credit debited/credited) from advancing on an economically unfavorable outcome.

### Likelihood Explanation
Likelihood is moderate-to-high: pool-price manipulation via ordinary swap/add-liquidity/remove-liquidity extrinsics is unprivileged and always available to any user of the chain, and `refund_weight` is invoked automatically by the XCM executor for very common message patterns (fees paid in non-native assets with weight overestimation), giving an attacker predictable trigger conditions without needing precise mempool timing (unlike the original off-chain sandwich scenario, an on-chain attacker can bracket the refund within the same block or adjacent blocks deterministically).

### Recommendation
Pass a non-`None` `amount_out_min` to the refund swap in `refund_weight`, e.g. derive it from `QuotePrice::quote_price_exact_tokens_for_tokens` with an acceptable slippage tolerance (mirroring how `buy_weight`/`quote_weight` already use `QuotePrice`), or skip the refund (return `None`) if the quoted minimum cannot be met, rather than executing an uncapped swap.

### Proof of Concept
1. Deploy/observe an Asset Hub-style runtime with `SwapFirstAssetTrader` configured as (part of) the `WeightTrader` and a `pallet-asset-conversion` pool for `(Target, refund_swap_asset)`.
2. Attacker (any account) submits a large swap or liquidity removal that shifts the pool reserves so that `Target -> refund_swap_asset` price temporarily worsens.
3. In the same or an adjacent block, a user submits an XCM message paying fees in `refund_swap_asset` with weight overestimation, triggering `SwapFirstAssetTrader::buy_weight` then `refund_weight`.
4. `refund_weight` executes `SwapCredit::swap_exact_tokens_for_tokens(..., None)` at the manipulated (unfavorable) rate — the user receives a below-market refund, and the AMM pool value differential is captured by the attacker when they reverse their initial trade.
5. Compare against `buy_weight`'s bounded swap and `pallet_asset_conversion::swap_exact_tokens_for_tokens`'s public dispatchable (which always enforces `amount_out_min`, see the `swap_should_not_work_if_too_much_slippage` test) to confirm only the `refund_weight` path lacks this guard. [4](#0-3)

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1092-1102)
```rust
				let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_out_min.map_or(true, |a| amount_out >= a),
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
				Ok((path, amount_out))
			};
			let (path, amount_out) = match inspect_path(credit_in.asset()) {
				Ok((p, a)) => (p, a),
				Err(e) => return Err((credit_in, e)),
			};
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L1565-1613)
```rust
#[test]
fn swap_should_not_work_if_too_much_slippage() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		create_tokens(user, vec![token_2.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));

		assert_ok!(Balances::force_set_balance(
			RuntimeOrigin::root(),
			user,
			10000 + get_native_ed()
		));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 1000));

		let liquidity1 = 10000;
		let liquidity2 = 200;

		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			liquidity1,
			liquidity2,
			1,
			1,
			user,
		));

		let exchange_amount = 100;

		assert_noop!(
			AssetConversion::swap_exact_tokens_for_tokens(
				RuntimeOrigin::signed(user),
				bvec![token_2.clone(), token_1.clone()],
				exchange_amount, // amount_in
				4000,            // amount_out_min
				user,
				false,
			),
			Error::<Test>::ProvidedMinimumNotSufficientForSwap
		);
	});
```
