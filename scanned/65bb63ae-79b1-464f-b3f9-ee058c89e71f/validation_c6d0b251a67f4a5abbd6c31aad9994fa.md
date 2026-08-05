## Analysis

The Deriverse bug is fundamentally: *a manipulable, un-cushioned "last trade price" of an on-chain market is trusted directly for a critical financial calculation, and an attacker can move that price and immediately benefit from the calculation that consumes it.* The closest local analog in `polkadot-sdk` is in `pallet-asset-conversion-tx-payment`, where the transaction-fee refund logic re-quotes the spot price of the **same** `pallet-asset-conversion` AMM pool *after* the user's own dispatched call has already executed and possibly re-priced that same pool — all within a single atomic extrinsic, with no TWAP/oracle and no independent slippage bound.

### Title
Same-block/same-extrinsic AMM spot-price manipulation drains `pallet-asset-conversion` pool via fee-refund conversion - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::correct_and_deposit_fee` converts the leftover native-fee refund back into the user's fee-payment asset by quoting `S::quote_price_exact_tokens_for_tokens` against the pool's **current** (post-dispatch) reserves and then immediately executing `S::swap_exact_tokens_for_tokens` against those same reserves. [1](#0-0)  If the dispatched call itself moves the reserves of that pool (e.g. a swap or liquidity operation on the very pool used for fee payment), the refund conversion is priced off an attacker-manipulated spot price with no external oracle, no TWAP, and no independent floor/ceiling — exactly the pattern flagged in the Deriverse report where `last_px` is trusted directly for a downstream financial computation.

### Finding Description
`ChargeAssetTxPayment::prepare` withdraws the fee upfront by converting the computed native `fee` into `asset_id` using the pool's spot price at validation/preparation time. [2](#0-1)  The dispatched call then executes. Afterwards, `post_dispatch_details` invokes `correct_and_deposit_fee` with the *unused* portion of the pre-charged native fee (`refund_amount`). [3](#0-2) 

Inside `correct_and_deposit_fee`, the refund path re-queries `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` to determine how much `asset_id` to give back to the user, then immediately executes the swap for that quoted amount: [4](#0-3) 

Both the quote and the executing swap read `pallet-asset-conversion`'s live pool reserves via `get_reserves`/`get_amount_out`, which are just the pool account's raw token balances at that moment — a pure spot price with no time-averaging. [5](#0-4) [6](#0-5) 

If a user picks `asset_id` such that its pool with the native asset `A` is the same pool their dispatched `call` interacts with (e.g. the call itself is `AssetConversion::swap_tokens_for_exact_tokens` or `remove_liquidity` on that pool), the reserves used for the pre-dispatch fee-withdraw quote and the reserves used for the post-dispatch refund quote/swap can differ dramatically — the second one reflecting a state the attacker just engineered inside the very same atomic extrinsic. There is no oracle, no TWAP, and no sanity bound comparing the refund conversion rate to the rate used at withdrawal time; the code only checks `can_deposit` succeeds, not that the rate is fair. [7](#0-6) 

### Impact Explanation
By designing a call whose dispatch weight is declared high (inflating the pre-charged `fee`/`refund_amount` headroom) and whose execution also swaps a large amount within the exact pool used for fee payment, an unprivileged user can cause the refund conversion to be priced at an artificially favorable rate, extracting more `asset_id` from the pool than the refunded native amount is actually worth. This is a direct value-conservation violation of pooled liquidity-provider funds — real assets leave the pool to the attacker beyond what the pre-manipulation exchange rate would justify, without needing any validator, collator, relayer, or governance actor. This falls squarely within the "Balances, assets... must conserve value and settle exactly once to the rightful beneficiary and amount" impact class.

### Likelihood Explanation
The attack requires only a signed account, sufficient capital to temporarily skew the target pool (which can be borrowed/returned within the same call via existing swap/liquidity extrinsics), and a chain/runtime that has `pallet-asset-conversion-tx-payment` configured with `SwapAssetAdapter` (this is the concrete adapter shipped in-tree and used in reference runtimes such as Asset Hub). No race condition, no cross-block MEV, and no interaction with other users is needed — everything happens deterministically inside one extrinsic's execution, so likelihood is high wherever this configuration is deployed with non-trivial pool depth relative to attacker capital.

### Recommendation
- Do not re-derive the refund conversion rate from post-dispatch pool state that the same extrinsic could have just perturbed. Cache/reuse the rate (or a bounded worst-case rate) captured at `withdraw_fee` time, or
- Bound the refund swap by a maximum acceptable slippage relative to the original withdrawal rate, rejecting/skipping the refund-swap (falling back to keeping the native refund undistributed or refunding in native) if the pool price has moved beyond a safe threshold since `withdraw_fee`.
- More generally, disallow using a pool as the fee-payment asset pool if the dispatched call is permitted to mutate that same pool's reserves within the same extrinsic, or snapshot/lock reserves for the fee computation for the duration of the extrinsic.

### Proof of Concept
1. Attacker funds a small liquidity pool `(Native, X)` used as `asset_id = X` for `ChargeAssetTxPayment`.
2. Attacker submits a signed extrinsic with `ChargeAssetTxPayment { asset_id: Some(X) }` wrapping a `call` that is itself `AssetConversion::swap_tokens_for_exact_tokens` (or `remove_liquidity`) draining most of the `Native` reserve of the same `(Native, X)` pool, and sets `info.weight` high enough that the pre-charged `fee` (and thus refund headroom) is large.
3. `prepare` withdraws `asset_fee` of `X` from the attacker at the pool's pre-manipulation spot price (`withdraw_fee`, `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:119-176`).
4. The dispatched call executes, draining `Native` from the pool, sharply raising the `X`-per-`Native` exchange rate.
5. `post_dispatch_details` -> `correct_and_deposit_fee` computes `refund_asset_amount` via `quote_price_exact_tokens_for_tokens` against the now-skewed reserves and immediately swaps `refund_amount` of `Native` into `X` at that skewed rate, paying the attacker an inflated amount of `X` pulled from the pool (`payment.rs:259-297`).
6. Net effect: attacker recovers more `X` value than the native refund was worth pre-manipulation, at the expense of the pool's liquidity providers, repeatable every block.

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-297)
```rust
		// refund is non zero and `who`'s fee `asset_id` is not the target asset.

		// check if the refund amount can be swapped back into `who`'s fee `asset_id`.
		let refund_asset_amount =
			S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
				// No refund given if it cannot be swapped back.
				.unwrap_or(Zero::zero());

		// `fee_paid` cannot be swapped back into `who`'s fee `asset_id` or the refund amount cannot
		// be deposited into `who`'s fee `asset_id`, exit without refund.
		if refund_asset_amount.is_zero() ||
			!matches!(
				F::can_deposit(asset_id.clone(), who, refund_asset_amount, Provenance::Extant),
				DepositConsequence::Success
			) {
			let (tip, fee) = fee_paid.split(tip);
			OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
			return Ok(fee_asset_amount);
		}

		// swap the refund amount back into `who`'s fee `asset_id`.

		let (refund, adjusted_paid) = fee_paid.split(refund_amount);

		let (fee_asset_amount, adjusted_paid) = match S::swap_exact_tokens_for_tokens(
			vec![A::get(), asset_id],
			refund,
			Some(refund_asset_amount),
		) {
			Ok(refund_asset) => match F::resolve(who, refund_asset) {
				Ok(_) => (fee_asset_amount.saturating_sub(refund_asset_amount), adjusted_paid),
				Err(refund_asset) => {
					defensive!(
						"Refund resolve should pass since `can_deposit` was checked",
						(refund_asset.asset(), refund_asset.peek(), who)
					);
					(fee_asset_amount, adjusted_paid)
				},
			},
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L389-410)
```rust
			InitialPayment::Asset((asset_id, already_withdrawn)) => {
				// Take into account the weight used by this extension before calculating the
				// refund.
				let actual_ext_weight = <T as Config>::WeightInfo::charge_asset_tx_payment_asset();
				let unspent_weight = extension_weight.saturating_sub(actual_ext_weight);
				let mut actual_post_info = *post_info;
				actual_post_info.refund(unspent_weight);
				let actual_fee = pallet_transaction_payment::Pallet::<T>::compute_actual_fee(
					len as u32,
					info,
					&actual_post_info,
					tip,
				);
				let converted_fee = T::OnChargeAssetTransaction::correct_and_deposit_fee(
					&who,
					info,
					&actual_post_info,
					actual_fee,
					tip,
					asset_id.clone(),
					already_withdrawn,
				)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1514)
```rust
		pub fn get_reserves(
			asset1: T::AssetKind,
			asset2: T::AssetKind,
		) -> Result<(T::Balance, T::Balance), Error<T>> {
			let pool_account = T::PoolLocator::pool_address(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			let balance1 = Self::get_balance(&pool_account, asset1);
			let balance2 = Self::get_balance(&pool_account, asset2);

			if balance1.is_zero() || balance2.is_zero() {
				Err(Error::<T>::PoolEmpty)?;
			}

			Ok((balance1, balance2))
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1547)
```rust
		pub fn quote_price_exact_tokens_for_tokens(
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

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
			};
```
