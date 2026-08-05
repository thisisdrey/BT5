## Analysis

The report's core broken invariant: a value used to determine an unprivileged actor's economic weight/entitlement is computed from **instantaneous, attacker-influenceable pool reserves within the same atomic transaction context**, with no time delay, TWAP, or staleness check, letting an attacker temporarily skew the ratio to their benefit and revert it immediately afterward at near-zero net cost.

The strongest local analog is `SwapAssetAdapter::withdraw_fee` / `correct_and_deposit_fee` in `pallet-asset-conversion-tx-payment`, which prices a transaction's fee-asset conversion using `S::quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens` — both of which read the **live, mutable pool reserves** of `pallet-asset-conversion` at call time [1](#0-0) . Unlike the nomination-pools points/balance ratio (which is only mutated by real, durable bonds/unbonds subject to unbonding-era delay) or the conviction-voting weight (which is locked concurrently with the vote and thus not flash-reversible), this fee-quoting path is evaluated purely from the pool's current on-chain reserves, and those reserves can be moved by ordinary `swap_exact_tokens_for_tokens` calls that any account can dispatch in a prior extrinsic within the same block, then reversed in a later extrinsic in the same block — a textbook flash-loan-style single-block reserve manipulation.

### Title
Transaction fee conversion via `SwapAssetAdapter` prices using unprotected instantaneous AMM reserves, enabling same-block fee-conversion manipulation - (File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs)

### Summary
`SwapAssetAdapter::withdraw_fee` and `correct_and_deposit_fee` determine how much of a non-native asset a signed account must pay (and how much refund it receives) for transaction fees by calling `quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens`, which read the pool's current reserves in `pallet-asset-conversion` with no staleness/TWAP protection [1](#0-0) . Because reserves can be moved arbitrarily by a preceding swap extrinsic in the same block and reverted afterward, an attacker can distort the price at which they pay/refund fees, mirroring the report's core primitive: an unprivileged actor briefly skewing a spot-derived ratio to extract value, then reverting the skew.

### Finding Description
`do_add_liquidity`, `do_remove_liquidity`, and `do_swap_exact_tokens_for_tokens` in `pallet-asset-conversion` all read reserves via `get_balance`/`get_reserves` at call time with no oracle smoothing [2](#0-1) [3](#0-2) . `quote_price_tokens_for_exact_tokens`, used directly by the fee adapter, likewise reads `Self::get_reserves(...)` live and applies `get_amount_in`/`quote` against those instantaneous balances [4](#0-3) .

`SwapAssetAdapter::withdraw_fee` uses this quote to compute `asset_fee` — the amount of the user-chosen asset that will be withdrawn and swapped for the fee-target asset `A` — directly from that live quote [5](#0-4) . `correct_and_deposit_fee` similarly uses `quote_price_exact_tokens_for_tokens` to determine the refund conversion back into the user's asset [6](#0-5) .

There is no mechanism analogous to the report's suggested fix (delayed effect / TWAP) protecting this price read: an attacker can, within one block (or via `pallet_utility::batch`/`batch_all` in a single extrinsic, or across an operator-controlled sequence of transactions they submit together), first execute a large `swap_exact_tokens_for_tokens` against the same pool used for their fee asset to push reserves in the direction that minimizes their required `asset_fee` for a given native `fee`, submit/execute the fee-paying transaction while the skewed price is in effect, then swap back to restore the original ratio, paying only the AMM's slip-based swap fee (`LPFee`/per-pool fee) as cost. This is functionally identical to the SPARTAN `getPoolShareWeight` attack: an instantaneous, single-block-reversible reserve read is used as the input to a value calculation (there: voting/reward weight; here: fee-asset pricing), with no delay-based defense.

Existing guards do not stop this path:
- `quote_price_tokens_for_exact_tokens` only checks that the pool has non-zero balance and enough reducible output, not that the price is stable/un-manipulated [7](#0-6) .
- The swap fee (`LPFee`/`PoolFees`) makes manipulation costly but not prohibitive for a sufficiently large fee-paying transaction relative to the manipulated pool's depth — the same "slip-based fee" caveat the original report explicitly calls out as insufficient.
- `already_withdrawn`/`change.peek().is_zero()` checks in `withdraw_fee` only assert the swap matched the (already-skewed) quote; they do not detect that the quote itself was manipulated [8](#0-7) .

### Impact Explanation
An attacker paying transaction fees via a shallow/thinly-traded pool asset can significantly reduce (or, on the refund side, inflate) the amount of the fee-asset actually consumed, at the cost of only the AMM swap fee. Repeated at scale (e.g., automated across many transactions or against a specific low-liquidity pool the attacker controls most of the liquidity in), this results in systematic underpayment of transaction fees in the intended fee-asset relative to fair market price, i.e., "public underpriced work" — the transaction is processed by the chain for less real economic value than intended, which is explicitly in-scope under the "public underpriced work that degrades block production" impact category.

### Likelihood Explanation
Any unprivileged, signed account can submit ordinary `pallet-asset-conversion` swap extrinsics and `ChargeAssetTxPayment`-covered extrinsics; no governance, admin, relayer, or validator collusion is required. The only cost is the round-trip AMM slippage fee on the manipulation swaps, which is bounded and can be minimized by targeting low-liquidity pools or splitting trades, exactly as described in the original report's attack scenario steps 2 and 6. Likelihood is moderate-to-high for pools with limited depth, but naturally self-limiting on very deep pools since manipulation cost scales with reserve size.

### Recommendation
Do not price fee-asset conversion, or at minimum the refund path, purely from the pool's instantaneous reserves read within the same transaction. Consider: (a) using a time-weighted or previous-block-checkpointed price for `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` when used specifically for fee conversion, (b) bounding the maximum single-block reserve deviation allowed before a fee-quote is considered stale and the extrinsic rejected, or (c) requiring a minimum notice/settlement delay between a swap that materially moves a pool's reserves and that pool's reserves being usable for fee-asset pricing, mirroring the report's suggested mitigation of delaying the effect of a manipulated ratio.

### Proof of Concept
1. Attacker holds asset `X` and a small amount of native asset `A`; a `pallet-asset-conversion` pool `X/A` exists with modest liquidity.
2. In extrinsic 1 (or a `pallet_utility::batch_all` bundling), attacker calls `AssetConversion::swap_exact_tokens_for_tokens` swapping a large amount of `A` into `X`, pushing the `X` reserve up and `A` reserve down, which lowers the marginal `A`-cost of `X` (i.e., makes `quote_price_tokens_for_exact_tokens(X, A, fee, true)` return a smaller `asset_fee` for the same native `fee`).
3. In the following extrinsic (or same batch, subject to inclusion ordering within the attacker's control), attacker submits a normal transaction using `ChargeAssetTxPayment::from(tip, Some(X))`; `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(X, A, fee, true)` against the now-skewed pool and withdraws a reduced amount of `X` from the attacker relative to the pool's undisturbed price.
4. In a final extrinsic, attacker swaps back `X` to `A` to restore the pool ratio, paying only the round-trip `LPFee`.
5. Net result: the attacker paid less `X` (real value) for the same native-denominated transaction fee than an honest user trading against the undisturbed pool price would have, at the cost of bounded AMM slippage — a direct analog to the SPARTAN `getPoolShareWeight` flash-loan manipulation, substituting "pool share weight" for "fee-asset conversion rate."

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-157)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;

		// Withdraw the `asset_id` credit for the swap.
		let asset_fee_credit = F::withdraw(
			asset_id.clone(),
			who,
			asset_fee,
			Precision::Exact,
			Preservation::Preserve,
			Fortitude::Polite,
		)
		.map_err(|_| InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L159-175)
```rust
		let (fee_credit, change) = match S::swap_tokens_for_exact_tokens(
			vec![asset_id, A::get()],
			asset_fee_credit,
			fee,
		) {
			Ok((fee_credit, change)) => (fee_credit, change),
			Err((credit_in, _)) => {
				defensive!("Fee swap should pass for the quoted amount");
				let _ = F::resolve(who, credit_in).defensive_proof("Should resolve the credit");
				return Err(InvalidTransaction::Payment.into());
			},
		};

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);

		Ok((fee_credit, asset_fee))
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L262-266)
```rust
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

```

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L913-920)
```rust
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

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
