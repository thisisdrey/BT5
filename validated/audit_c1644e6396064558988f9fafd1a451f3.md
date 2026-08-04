### Title
Unbounded-slippage swap in XCM fee refund path enables sandwich extraction of parachain fee refunds - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader`, used as a `WeightTrader` in Asset Hub (Rococo/Westend) and Penpal XCM configurations, swaps a user's unused XCM execution fee back into their original asset via `SwapCredit::swap_exact_tokens_for_tokens` with `amount_out_min` hard-coded to `None`. This is the direct analog of the reported `liquidateToken()` bug: a swap executed with zero slippage protection, sandwichable by anyone able to move the `pallet-asset-conversion` pool reserves before the refund executes.

### Finding Description
`buy_weight` correctly swaps a user's asset for the `Target` fee asset using `swap_tokens_for_exact_tokens`, bounded by the exact `fee` amount, with no explicit min/max supplied because it swaps for an exact output. However `refund_weight` — which returns unused weight fees back to the user in their original asset — calls: [1](#0-0) 

```
let refund = self.total_fee.extract(refund_amount);
let refund = match SwapCredit::swap_exact_tokens_for_tokens(
    vec![Target::get(), refund_swap_asset],
    refund,
    None,
) {
```

The `amount_out_min` parameter of `SwapCredit::swap_exact_tokens_for_tokens` is explicitly passed as `None`, disabling the `Error::<T>::ProvidedMinimumNotSufficientForSwap` guard implemented in `pallet_asset_conversion::Pallet::do_swap_exact_credit_tokens_for_tokens`: [2](#0-1) 

That guard exists specifically to protect callers from receiving less than expected due to price movement between quote and execution — exactly the protection the report calls for. `refund_weight` deliberately opts out of it, so the refund swap accepts *any* non-zero output amount from the pool.

Because `pallet-asset-conversion` pools are public AMM pools (Uniswap V2-style, see `substrate/frame/asset-conversion/src/lib.rs:18-30`), any account can submit ordinary `swap_exact_tokens_for_tokens`/`add_liquidity`/`remove_liquidity` extrinsics against the same `Target`/`refund_swap_asset` pool in the same block, shifting the exchange rate immediately before the XCM message executes and the trader's `refund_weight` fires, then reversing the trade immediately after — a textbook sandwich attack. This requires no privileged role, no malicious validator/collator/relayer, and no admin action — only an ordinary extrinsic sender racing the XCM executor within a block, which is exactly the "unprivileged attacker" primitive the report describes.

### Impact Explanation
The victim is the account whose incoming XCM message (e.g., a reserve-asset transfer or remote-execution message) is charged fees through `SwapFirstAssetTrader`. Their unused-weight refund, which should return close to the correctly quoted amount of their original asset, can be minimized by an attacker manipulating the pool's spot price around the block in which the refund swap executes. This is public, underpriced/unprotected value extraction from ordinary users interacting with Asset Hub's XCM executor, matching the "public underpriced work" and "theft ... of unbacked mint/unlock"-adjacent impact class (value siphoned from the protocol's fee-refund mechanism to an MEV actor) without needing any privileged actor.

Confirmed live usage of `SwapFirstAssetTrader` in the `Trader` configuration of: [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

(I could not fully verify exact line numbers of the `Trader` tuple wiring in each xcm_config.rs within this session — only the grep hit location is confirmed; a Devin session with full file access should confirm the precise construction to rule out any wrapping min-amount enforcement at a higher layer.)

### Likelihood Explanation
Medium-high. It requires only ordinary signed extrinsics against a public AMM pool, timed relative to XCM message execution within the same block — well within reach of any MEV-aware actor monitoring the mempool/XCMP queue, with no reliance on validator, collator, or relayer misbehavior. The main mitigating factor is that refund amounts are typically small (unused weight fees), which may reduce attacker profit per instance, but the guard removal (`None` instead of a computed floor) is unconditional and applies to every refund on every affected chain.

### Recommendation
Compute a slippage floor for the refund swap analogous to what `buy_weight` already achieves implicitly via exact-output swap: quote the expected output with `QuotePrice::quote_price_exact_tokens_for_tokens` before calling `swap_exact_tokens_for_tokens`, and pass `Some(min_expected.saturating_sub(tolerance))` instead of `None`. If the swap cannot clear that minimum, keep the original `Target`-asset credit in `total_fee` rather than force through an unprotected swap (mirroring the existing error-handling fallback path already present in `refund_weight`, which already handles swap failure gracefully).

### Proof of Concept
1. Deploy/observe Asset Hub with `pallet-asset-conversion` pool for `(Target, refund_swap_asset)` with modest liquidity.
2. Attacker submits `swap_exact_tokens_for_tokens` (or `add_liquidity`/`remove_liquidity`) pushing the pool's `Target -> refund_swap_asset` price sharply against the expected refund direction, included in the same block just before the victim's XCM message.
3. Victim's inbound XCM message is executed; `SwapFirstAssetTrader::buy_weight` charges fees in `refund_swap_asset`, converts to `Target`; upon completion, `refund_weight` is invoked for unused weight, calling `SwapCredit::swap_exact_tokens_for_tokens(vec![Target, refund_swap_asset], refund, None)` — the deflated pool price is accepted unconditionally since `amount_out_min = None`.
4. Attacker submits a second extrinsic in the same or the next block restoring the pool price, having captured the value the victim should have received.

Note: I was not able to execute or run this scenario in a live/test environment within this session (no execution access); this PoC is derived purely from static code analysis of the cited functions in `cumulus/primitives/utility/src/lib.rs` and `substrate/frame/asset-conversion/src/lib.rs`. A Devin session with build/test tooling would be needed to reproduce this concretely against `asset-hub-westend`/`asset-hub-rococo` integration tests.

### Citations

**File:** cumulus/primitives/utility/src/lib.rs (L539-544)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1086-1096)
```rust
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/xcm_config.rs (L1-1)
```rust
// This file is part of Cumulus.
```

**File:** substrate/frame/staking-async/runtimes/parachain/src/xcm_config.rs (L1-1)
```rust
// This file is part of Substrate.
```
