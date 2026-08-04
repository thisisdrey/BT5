## Analysis

The seed report's core issue is: **an AMM swap executes without an enforced minimum-output (or maximum-input) bound, so a party manipulating the pool around the swap can extract the difference between fair price and executed price at the victim's expense.**

`pallet-asset-conversion` itself does the right thing: its public extrinsics `swap_exact_tokens_for_tokens` / `swap_tokens_for_exact_tokens` and the `SwapCredit`/`Swap` trait methods always take an `Option<Balance>` slippage bound and enforce it via `Error::ProvidedMinimumNotSufficientForSwap` / `ProvidedMaximumNotSufficientForSwap`. [1](#0-0) 

However, a concrete internal consumer of `SwapCredit` bypasses this protection: `SwapFirstAssetTrader`, the XCM `WeightTrader` used to let a message pay execution/delivery fees in a non-native asset by swapping it for the `Target` asset through the pool. Its `refund_weight` implementation converts unspent `Target` fee back into the original payment asset with **no minimum-output bound at all** — `None` is passed explicitly where the slippage parameter belongs: [2](#0-1) 

This is the direct local analog of the seed bug: the AMM interface (`SwapCredit::swap_exact_tokens_for_tokens`) does support a slippage-protecting parameter, but this specific caller intentionally omits it, so the refund can be executed at an arbitrarily bad price if the pool's reserves are skewed at execution time (e.g., by ordinary swap extrinsics against the same pool placed around the same block as the fee-bearing XCM message). `SwapFirstAssetTrader` is wired into live runtimes (Asset Hub Westend/Rococo, Penpal, staking-async parachain template) and is also used for paying XCM delivery fees across chains via Asset Hub, per `prdoc/1.16.0/pr_5131.prdoc`. [3](#0-2) 

By contrast, the `buy_weight` path is naturally bounded because the amount swapped in is capped by the fixed `credit_in` (the payment attached to the message), so the analogous exposure is narrower there. The unbounded exposure is specific to `refund_weight`'s swap-back leg.

### Title
Unbounded-slippage refund swap in `SwapFirstAssetTrader::refund_weight` - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader::refund_weight` swaps the unused `Target` fee credit back into the asset the message paid with by calling `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min = None`. Unlike `pallet-asset-conversion`'s public dispatchables, which always allow and, when the caller wants it, enforce a minimum-output bound, this internal call explicitly disables that protection for a swap performed autonomously during XCM execution.

### Finding Description
`SwapFirstAssetTrader` lets an XCM message pay fees in an asset other than `Target` by swapping it via `SwapCredit`. When a refund is due (because less weight was actually consumed than was bought), `refund_weight` swaps `Target` back into the client's original asset:
```rust
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
)
``` [4](#0-3) 

The `None` disables the `ProvidedMinimumNotSufficientForSwap` guard that exists specifically to prevent this class of loss: [5](#0-4) 

Because the swap executes with whatever spot price the pool has at that moment in block execution, and pool reserves can be moved by ordinary, unprivileged `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics from any account against the same pool, the corrupted value is the `credit_out` amount returned to the fee payer in `refund_weight`. No existing guard bounds how small this can become relative to fair value — the call site itself opts out of the one mechanism (`amount_out_min`) that would otherwise cap the loss.

### Impact Explanation
This directly reduces the value returned to the account whose XCM message overpaid fees, and correspondingly lets whoever skews the pool at the right moment capture the spread. Since `SwapFirstAssetTrader` also backs delivery-fee payment across parachains via Asset Hub, the same pattern can degrade the economics of cross-chain fee handling without needing a malicious relayer, validator, or collator — any unprivileged account can submit ordinary swap extrinsics against the shared pool.

### Likelihood Explanation
Exploitability only requires: (1) a configured pool between `Target` and the refund asset with limited liquidity, and (2) an unprivileged account able to submit swaps in the same block/window as the refund. Both are ordinary, publicly available operations; no privileged role or off-chain compromise is needed.

### Recommendation
Compute an expected refund amount using `QuotePrice::quote_price_exact_tokens_for_tokens` (already used elsewhere in this file for `quote_weight`) before the swap, and pass a derived `amount_out_min` (with a configurable tolerance) into `SwapCredit::swap_exact_tokens_for_tokens` instead of `None`, mirroring the protection already implemented for the public `pallet-asset-conversion` extrinsics.

### Proof of Concept
1. Configure `SwapFirstAssetTrader` with `Target = DOT` and a pool `(DOT, USDC)` with shallow liquidity, as in the Asset Hub test setups. [6](#0-5) 
2. Submit an XCM message that overpays fees in `USDC`, causing `buy_weight` to swap into `DOT`, followed by a `refund_weight` call for the unused portion.
3. Before the refund executes (or, on Substrate, within block construction ordering the attacker influences via ordinary transaction submission and fees), the attacker calls `AssetConversion::swap_exact_tokens_for_tokens` to shift the `(DOT, USDC)` pool's price against the refund direction, then reverses it afterward.
4. Because `refund_weight` passes `None` for `amount_out_min`, the refund executes at the manipulated price, returning materially less `USDC` to the original fee payer than fair value, with the difference capturable by the attacker's second (reversing) trade.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1002)
```rust
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```

**File:** cumulus/primitives/utility/src/lib.rs (L538-546)
```rust

		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
			Err((refund, _)) => {
```

**File:** prdoc/1.16.0/pr_5131.prdoc (L1-24)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Swap for paying delivery fees in different assets

doc:
  - audience: Runtime User
    description: |
      If the `AssetExchanger` is configured on a runtime, the XCM executor is now able to swap assets
      to pay for delivery fees.
      This was already possible for execution fees via the `SwapFirstAssetTrader`.
      A runtime where this will be possible is Asset Hub.
      That means reserve asset transfers from Parachain A to Parachain B passing through Asset Hub no
      longer need to have any DOT to pay for fees on AssetHub.
      They can have any asset in a pool with DOT on Asset Hub, for example USDT or USDC.
  - audience: Runtime Dev
    description: |
      Using the `AssetExchanger` XCM config item, the executor now swaps fees to use for delivery fees,
      if possible.
      If you want your runtime to support this, you need to configure this new item.
      Thankfully, `xcm-builder` now has a new adapter for this, which lets you use `pallet-asset-conversion`
      or any type that implements the `SwapCredit` and `QuotePrice` traits.
      It's called `SingleAssetExchangeAdapter`, you can read more about it in its rust docs.
      This item is already configured in Asset Hub.
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/tests/tests.rs (L281-306)
```rust
			assert_ok!(AssetConversion::create_pool(
				RuntimeHelper::origin_of(bob.clone()),
				Box::new(
					xcm::v5::Location::try_from(native_location.clone()).expect("conversion works")
				),
				Box::new(
					xcm::v5::Location::try_from(asset_1_location.clone())
						.expect("conversion works")
				)
			));

			assert_ok!(AssetConversion::add_liquidity(
				RuntimeHelper::origin_of(bob.clone()),
				Box::new(
					xcm::v5::Location::try_from(native_location.clone()).expect("conversion works")
				),
				Box::new(
					xcm::v5::Location::try_from(asset_1_location.clone())
						.expect("conversion works")
				),
				pool_liquidity,
				pool_liquidity,
				1,
				1,
				bob.clone(),
			));
```
