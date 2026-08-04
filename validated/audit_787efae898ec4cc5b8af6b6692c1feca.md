### Title
Instantaneous spot-price (zero-window) quote used to price transaction fees enables single-block price manipulation - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
The external report flags a 30-second TWAP lookback in `LaunchFactory.createTokenManager()` as too short, allowing price manipulation of a value used for a critical financial decision. The structural analog in this repository is worse: `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` prices transaction fees using `pallet-asset-conversion`'s `quote_price_tokens_for_exact_tokens`, which reads the AMM pool's *current* reserves with no time-weighting or averaging window at all (i.e., a "0-second lookback" instead of a too-short one).

### Finding Description
`QuotePrice::quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens` in `substrate/frame/asset-conversion/src/lib.rs` (lines 1523-1604) compute prices directly from `Self::get_reserves(asset1, asset2)` — the pool's live balances at the moment of the call — with no TWAP, no minimum observation window, and no price-impact cap: [1](#0-0) 

The trait doc for `QuotePrice` in `substrate/frame/asset-conversion/src/swap.rs` explicitly acknowledges this is an instantaneous quote and is only reliable "if no other swaps are made after the price is quoted and before the target swap": [2](#0-1) 

This spot price is consumed directly to determine how much of a non-native asset a user pays for transaction fees in `SwapAssetAdapter::withdraw_fee` and `can_withdraw_fee` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`): [3](#0-2) 

Because extrinsics in a block execute sequentially and each extrinsic's `withdraw_fee` reads the pool's reserves as they stand *at that moment* (not an average over any window), an attacker who controls transaction ordering within a block (e.g., via tips, or simply by being first in the block) can:
1. Submit a swap extrinsic against the same asset-conversion pool that skews the `asset_id`/native reserve ratio in their favor.
2. Immediately submit their fee-paying extrinsic (using `asset_id` as the fee asset), which will be quoted against the now-skewed reserves, letting them pay an artificially small amount of `asset_id` for the required native-equivalent fee.
3. Optionally reverse the first swap in a later extrinsic (or let arbitrageurs correct it), realizing underpriced transaction fees at near-zero net cost.

Existing guards do not stop this path:
- `quote_price_tokens_for_exact_tokens` only guards against zero-liquidity (`balance1.is_zero()`) and against exceeding `reducible_balance` (min-balance preservation) — it applies no slippage/impact bound relative to a historical or averaged price.
- `withdraw_fee`/`can_withdraw_fee` only filter out a zero quote (`!asset_fee.is_zero()`); they do not compare against any reference/TWAP price to detect abnormal deviation.
- The swap itself (`swap_tokens_for_exact_tokens`) executes atomically for the *quoted* amount, so it always "succeeds" even when the quote itself was manipulated moments earlier by a separate extrinsic in the same block.

### Impact Explanation
This falls under the "public underpriced work that degrades block production" impact category: an attacker can pay drastically reduced effective fees (in real/native value terms) for transaction inclusion by manipulating the AMM pool used for fee conversion just before their fee-paying extrinsic executes, using only unprivileged, permissionless swap calls against a public liquidity pool (no admin, governance, validator, or relayer role required). This can be used to spam the network at below-market cost, undermining the fee market's DoS-resistance property, and can also cause honest users paying fees via the same asset/pool in nearby blocks to be over- or under-charged relative to fair market price.

### Likelihood Explanation
Likelihood is high for any chain that enables `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` against thinly-liquidated or attacker-accessible pools (e.g., pools an attacker can size relative to their own capital), since the only requirement is submitting ordinary, permissionless swap and fee-paying extrinsics within the same block — no privileged access, off-chain infrastructure, or malicious node/validator/relayer assumption is needed. The severity scales inversely with pool depth relative to attacker capital, similar to how a short TWAP window scales manipulation risk inversely with the attacker's capacity to move price within that window.

### Recommendation
Do not use the raw instantaneous AMM spot price from `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` directly for fee pricing. Introduce a time-weighted or multi-block averaged price (or at minimum a maximum allowed deviation from a recent reference price / price-impact cap) before using the quote in `SwapAssetAdapter::withdraw_fee` and `can_withdraw_fee`, analogous to increasing a TWAP lookback window, so that a single-block/single-extrinsic reserve shift cannot materially affect the fee actually charged.

### Proof of Concept
1. Attacker deploys/holds a large balance of `asset_id` and native token, and there exists a `pallet-asset-conversion` pool for `(asset_id, Native)` used as the fee-conversion route in `SwapAssetAdapter`.
2. In block N, attacker submits extrinsic 1: a large `swap_exact_tokens_for_tokens` (or `swap_tokens_for_exact_tokens`) that shifts the pool reserves so that `quote_price_tokens_for_exact_tokens(asset_id, Native, fee, true)` (see `substrate/frame/asset-conversion/src/lib.rs:1571-1602`) now returns a much smaller `asset_fee` for the same native `fee`.
3. In the same block N (ordered immediately after via tip or transaction-pool priority), attacker submits extrinsic 2: any call wrapped by `ChargeAssetTxPayment` paying fees in `asset_id`. `SwapAssetAdapter::withdraw_fee` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:119-146`) calls the manipulated quote and withdraws only the deflated `asset_fee` amount, then swaps it for the full native `fee` — the attacker effectively pays a fraction of the intended fee value.
4. Attacker submits extrinsic 3 (optional) reversing the initial swap to restore the pool and repeat the attack in subsequent blocks, or lets the arbitrage loss be absorbed by other pool LPs/traders.

Exact quantification of achievable discount depends on pool depth vs. attacker capital and constant-product AMM curve parameters (`get_amount_in`/`get_amount_out` in `substrate/frame/asset-conversion/src/lib.rs`), which I was not able to fully trace line-by-line within the available context; a Devin session with full repo/test access would be needed to build a concrete numeric PoC against `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs`'s `ExtBuilder`/`setup_lp` harness.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1571-1602)
```rust
		pub fn quote_price_tokens_for_exact_tokens(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
			amount: T::Balance,
			include_fee: bool,
		) -> Option<T::Balance> {
			// Swaps reject zero amounts, match that behavior.
			if amount.is_zero() {
				return None;
			}
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2).ok()?;

			let (balance1, balance2) = Self::get_reserves(asset1.clone(), asset2.clone()).ok()?;

			if balance1.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output =
				T::Assets::reducible_balance(asset2.clone(), &pool_account, Preserve, Polite);
			if amount > max_output {
				return None;
			}

			if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_in(fee, &amount, &balance1, &balance2).ok()
			} else {
				Self::quote(&amount, &balance2, &balance1).ok()
			}
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-134)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
	/// Measurement units of the asset classes for pricing.
	type Balance: Balance;
	/// Type representing the kind of assets for which the price is being quoted.
	type AssetKind;
	/// Quotes the amount of `asset1` required to obtain the exact `amount` of `asset2`.
	///
	/// If `include_fee` is set to `true`, the price will include the pool's fee.
	/// If the pool does not exist or the swap cannot be made, `None` is returned.
	fn quote_price_tokens_for_exact_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance>;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-146)
```rust
	fn withdraw_fee(
		who: &T::AccountId,
		_call: &T::RuntimeCall,
		_dispatch_info: &DispatchInfoOf<<T>::RuntimeCall>,
		asset_id: Self::AssetId,
		fee: Self::Balance,
		_tip: Self::Balance,
	) -> Result<Self::LiquidityInfo, TransactionValidityError> {
		if asset_id == A::get() {
			// The `asset_id` is the target asset, we do not need to swap.
			let fee_credit = F::withdraw(
				asset_id.clone(),
				who,
				fee,
				Precision::Exact,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.map_err(|_| InvalidTransaction::Payment)?;

			return Ok((fee_credit, fee));
		}

		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```
