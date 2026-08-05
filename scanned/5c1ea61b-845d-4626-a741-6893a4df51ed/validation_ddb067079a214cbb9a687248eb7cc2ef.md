### Title
`ChargeAssetTxPayment` fee conversion uses unprotected AMM spot price, enabling self-sandwich fee-asset extraction - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
The Curve-pool `price_oracle` bug is about a value-determining price feed (an EMA over trades) that lacks freshness/deviation guards, letting an attacker skew it a few blocks in advance to make a downstream contract move more/less value than it should. `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter` has the same broken invariant in miniature: it prices transaction fees paid in a non-native asset using `pallet_asset_conversion`'s **instantaneous spot price** (raw pool reserves), with no TWAP, no minimum liquidity check, and no bound on how much the quoted price may have moved since the fee was computed. An attacker fully controls this "oracle" for a single block by trading against the same pool with their own prior extrinsic.

### Finding Description
`SwapAssetAdapter::withdraw_fee` and `can_withdraw_fee` call `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)`, which resolves to `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens`: [1](#0-0) 

That quote function reads `get_reserves` directly — the pool's current on-chain balances — and derives `amount_in` from the constant-product formula, with no averaging, no staleness window, and no deviation check against a longer-run reference price: [2](#0-1) 

The doc comment itself flags the risk but only in the context of *slippage for voluntary swaps* ("price may have changed by the time the transaction is executed... use amount_in_max to control slippage") — there is no equivalent slippage control for the tx-payment path, because the user does not choose `fee`; it's fixed by `pallet_transaction_payment::compute_fee`.

Because `frame_system` enforces per-account nonce ordering within a block, an attacker who fully controls their own account's transaction sequence can, in one block:
1. Extrinsic N: swap native for `asset_id` in the target pool, moving reserves so that `asset_id`'s spot price against native is temporarily favorable to the attacker.
2. Extrinsic N+1: submit the fee-paying transaction with `asset_id` set as the fee asset — `withdraw_fee` re-quotes the now-skewed spot price and withdraws an artificially small amount of `asset_id` to cover the fixed native `fee`.
3. Extrinsic N+2: swap back, restoring the pool and recovering most of the capital used in step 1, keeping the difference extracted from the pool's liquidity providers.

No relayer, validator, collator, governance actor, or leaked key is needed — this is achievable purely by an ordinary account submitting three of its own extrinsics with sufficient tip/priority in the same block, exactly mirroring the "sophisticated attacker...skew the price a couple blocks before" pattern from the source report, compressed into a single-block, self-controlled window instead of requiring a flash loan or external mempool visibility.

### Impact Explanation
This breaks the "conserve value, settle exactly once to the rightful... amount" pivot for pool-held liquidity: LP reserves absorb the difference between the manipulated quote and the true price, i.e. value is extracted from liquidity providers via a mispriced, non-slippage-protected fee conversion. Because `fee` (the native-asset amount) is fixed independent of the spot price used to size the `asset_id` withdrawal, there is no natural bound analogous to a `amount_in_max`/`amount_out_min` check that would fail the extrinsic if the price had moved unfavorably for the pool.

### Likelihood Explanation
Any account holding both the native asset and the alternate fee asset can perform this without special privileges, in low/medium liquidity pools where the resulting price impact from steps 1 and 3 is affordable relative to the fee amounts extracted repeatedly, across many blocks. The attack requires no cooperation from block producers beyond normal inclusion of the attacker's own extrinsics in nonce order, which is the default behavior.

### Recommendation
- Require `withdraw_fee`/`can_withdraw_fee` to bound the deviation between the quote used at `validate`/`prepare` time and a reference price (e.g., a short TWAP over recent blocks or a maximum allowed price-impact ratio derived from pool depth), rejecting the extrinsic (`InvalidTransaction::Payment`) if exceeded.
- Alternatively, cap the amount of `asset_id` that `withdraw_fee` may withdraw per fee-quote to a value bounded by a longer-window average price rather than the instantaneous `get_reserves` snapshot, similar to the report's recommendation to check `pLast` vs `price_oracle` deviation before trusting the quote.

### Proof of Concept
Conceptual sequence (single block, single attacker account, using `ChargeAssetTxPayment` with `asset_id != A::get()`):
1. `swap_exact_tokens_for_tokens([Native, AssetX])` — push a large amount of native into the `Native/AssetX` pool to shift the spot ratio.
2. Submit the target extrinsic with `ChargeAssetTxPayment { asset_id: Some(AssetX), .. }`. During `prepare`, `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(AssetX, Native, fee, true)` against the now-skewed reserves from step 1, withdrawing less `AssetX` than the pre-manipulation price would have required.
3. `swap_exact_tokens_for_tokens([AssetX, Native])` — reverse the trade from step 1, restoring reserves and recovering the bulk of the native spent, net of pool fees, while having paid an artificially cheap fee in step 2. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L119-176)
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
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1564-1603)
```rust
		/// Gets a quote for swapping `amount` of `asset1` for an exact amount of `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_in_max` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
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
		}
```
