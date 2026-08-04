`SwapFirstAssetTrader` is configured live in `AssetHubRococo`, `AssetHubWestend`, `Penpal`, and `staking-async` parachain runtimes [1](#0-0) , so this is not a theoretical/test-only path.

### Title
`SwapFirstAssetTrader::refund_weight` executes an unbounded-slippage swap with `amount_out_min = None`, allowing fee refunds to be drained via pool-price manipulation - (File: `cumulus/primitives/utility/src/lib.rs`)

### Summary
`SwapFirstAssetTrader` is a `WeightTrader` used in live Asset Hub / Penpal / staking-async runtimes to let users pay XCM execution fees in a non-native asset by swapping it (via `pallet_asset_conversion`) into the `Target` fee asset. When unused weight is refunded at the end of message execution, `refund_weight` swaps the surplus `Target` credit back into the user's original asset by calling `SwapCredit::swap_exact_tokens_for_tokens(..., None)` — passing `None` for `amount_out_min`, i.e. no minimum-output/slippage bound, exactly the same broken invariant as the reported `bridgeFunds` bug (hardcoded `0` minimum output).

### Finding Description
`refund_weight` extracts the unused portion of `total_fee` (in `Target` asset) and swaps it back to the asset the user paid with: [2](#0-1) 

Unlike `buy_weight`, which uses `swap_tokens_for_exact_tokens` to obtain an exact fee amount, and unlike `quote_weight`, which calls `QuotePrice::quote_price_tokens_for_exact_tokens` to compute an expected rate, `refund_weight` performs a "swap exact tokens for tokens" with **no minimum acceptable output**. `pallet_asset_conversion::do_swap_exact_credit_tokens_for_tokens` accepts `amount_out_min: Option<Balance>` and, when `None`, skips the `ProvidedMinimumNotSufficientForSwap` check entirely [3](#0-2)  — the pool can return any nonzero amount, however small, and the swap still succeeds. The `SingleAssetExchangeAdapter` used for XCM's `ExchangeAsset` correctly always supplies `Some(want_amount)` as the floor [4](#0-3) , showing that `refund_weight` is the outlier that omits this guard.

Because pool reserves for `Target`/user-asset pairs are on-chain AMM state, an attacker can manipulate the exchange rate immediately before the refund swap executes within the same block (e.g., via a large adjacent extrinsic/XCM that moves the pool reserves), causing the refund conversion to return a value far below the fair rate. The difference is captured by the manipulating party as arbitrage, while the fee-payer's rightful refund is silently reduced — value leaves the intended beneficiary without any error being raised.

### Impact Explanation
This breaks the "Balances, assets ... must conserve value and settle exactly once to the rightful beneficiary and amount" invariant for a live, publicly reachable code path (any XCM message executed on Asset Hub/Penpal that pays fees via `SwapFirstAssetTrader` and leaves unused weight to refund). It causes silent underpayment/fund loss to XCM senders without reverting, and is a systemic pricing-integrity flaw in a widely deployed fee-payment primitive, not an isolated test/mock issue.

### Likelihood Explanation
Every XCM message that overestimates its weight limit (dry-run derived estimates commonly overshoot) and pays fees in a non-`Target` asset via `SwapFirstAssetTrader` triggers `refund_weight`. Exploitation requires only ordinary public actions — placing another swap/transaction against the same asset-conversion pool in the same block — not a malicious validator, collator, relayer, or governance actor, which is consistent with the "sandwich" mechanism explicitly called out in the source report as the qualifying attack class rather than a mere front-run.

### Recommendation
Compute an expected minimum via `QuotePrice::quote_price_exact_tokens_for_tokens` (analogous to how `quote_weight` already uses `quote_price_tokens_for_exact_tokens`) and pass `Some(min_acceptable_amount)` into `swap_exact_tokens_for_tokens` in `refund_weight`, falling back to keeping the `Target` asset (skipping the refund swap) if the quoted minimum cannot be met, mirroring the fix pattern from the external report (add and enforce a minimum-output parameter).

### Proof of Concept
1. Configure a runtime with `SwapFirstAssetTrader<Target=DOT, ...>` (as in Asset Hub) and a `DOT`/`USDT` pool in `pallet_asset_conversion`.
2. Attacker/user submits an XCM message with an inflated `BuyExecution` weight limit, paying in `USDT`; `buy_weight` swaps `USDT` → `DOT` to cover the estimated fee, leaving a large unused `total_fee` in `DOT` to be refunded in `USDT`.
3. In the same block, the attacker executes a large `USDT → DOT` swap against the same pool right before the refund is processed (or via message ordering within the block), depressing the `DOT → USDT` rate.
4. `refund_weight` calls `swap_exact_tokens_for_tokens(vec![DOT, USDT], refund, None)`; since `amount_out_min` is `None`, the swap succeeds despite returning far less `USDT` than the fair-rate refund would be.
5. The original sender receives a diminished refund; the attacker recoups the difference by reversing their pool-manipulating trade after the refund executes, extracting value from the fee-payer with no error/revert in the process.

### Citations

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/xcm_config.rs (L1-1)
```rust
// Copyright (C) Parity Technologies (UK) Ltd.
```

**File:** cumulus/primitives/utility/src/lib.rs (L539-545)
```rust
		let refund = self.total_fee.extract(refund_amount);
		let refund = match SwapCredit::swap_exact_tokens_for_tokens(
			vec![Target::get(), refund_swap_asset],
			refund,
			None,
		) {
			Ok(refund_in_target) => refund_in_target,
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1111-1127)
```rust
		/// Swaps a portion of `credit_in` of `path[0]` asset to obtain the desired `amount_out` of
		/// the `path[last]` asset. The provided `credit_in` must be adequate to achieve the target
		/// `amount_out`, or an error will occur.
		///
		/// On success, the function returns a (`credit_out`, `credit_change`) tuple, where
		/// `credit_out` represents the acquired amount of the `path[last]` asset, and
		/// `credit_change` is the remaining portion from the `credit_in`. On failure, an `Err` with
		/// the initial `credit_in` and error code is returned.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		pub(crate) fn do_swap_credit_tokens_for_exact_tokens(
			path: Vec<T::AssetKind>,
			credit_in: CreditOf<T>,
			amount_out: T::Balance,
		) -> Result<(CreditOf<T>, CreditOf<T>), (CreditOf<T>, DispatchError)> {
```

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L107-114)
```rust
		let (credit_out, maybe_credit_change) = if maximal {
			// If `maximal`, then we swap exactly `credit_in` to get as much of `want_asset_id` as
			// we can, with a minimum of `want_amount`.
			let credit_out = match <AssetConversion as SwapCredit<_>>::swap_exact_tokens_for_tokens(
				vec![swap_asset, want_asset_id],
				credit_in,
				Some(want_amount),
			) {
```
