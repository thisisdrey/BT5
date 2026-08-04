## Title
`ChargeAssetTxPayment` refunds the swapped-asset fee using post-dispatch pool reserves, letting a signed extrinsic arbitrage its own dispatched call for free value from `pallet-asset-conversion` pools - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
The `SwapAssetAdapter::correct_and_deposit_fee` refund path re-quotes the asset‑conversion pool price **after** the wrapped call has already executed and mutated that same pool's reserves, exactly mirroring the root cause of the reported Uniswap H‑17 bug: using the *current* AMM state instead of a manipulation-resistant reference price to size a swap that the protocol performs on the user's behalf.

### Finding Description
`ChargeAssetTxPayment` is a `TransactionExtension` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs`) that:
1. Pre-dispatch: calls `OnChargeAssetTransaction::withdraw_fee`, which quotes and immediately swaps `asset_id -> A` (native) for the estimated fee, atomically, at the pool's reserves at time T0.
2. Dispatches the user's `call`.
3. Post-dispatch: calls `correct_and_deposit_fee`, which computes a refund of `A` (native) back into the user's `asset_id`.

The refund conversion in `SwapAssetAdapter::correct_and_deposit_fee` (`substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs:262-286`) does:
```rust
let refund_asset_amount =
    S::quote_price_exact_tokens_for_tokens(A::get(), asset_id.clone(), refund_amount, true)
        .unwrap_or(Zero::zero());
...
S::swap_exact_tokens_for_tokens(vec![A::get(), asset_id], refund, Some(refund_asset_amount))
```
`S` is `pallet_asset_conversion::Pallet<T>`, whose `quote_price_exact_tokens_for_tokens`/`swap_exact_tokens_for_tokens` read the pool's **live reserves** via `get_reserves` (`substrate/frame/asset-conversion/src/lib.rs:1495-1562`) - there is no TWAP, oracle, or price-at-withdrawal snapshot used for this second quote.

If the user's own dispatched `call` is itself a swap/liquidity operation on the *same* `(A, asset_id)` pool (e.g. `AssetConversion::swap_exact_tokens_for_tokens`, `add_liquidity`, `remove_liquidity`), it moves the pool's reserves between the pre-dispatch withdrawal (T0) and the post-dispatch refund quote (T1) — all inside the same atomic extrinsic, executed by an ordinary unprivileged signed account. The refund-back swap at T1 then executes at a reserve ratio the *same signer* just engineered, letting them extract value from liquidity providers by choosing `refund_amount`'s conversion rate advantageously (analogous to steps 1–3 of the Uniswap PoC: move price, get favorable settlement, move price back — here "moving back" isn't even required because it's the attacker's own transaction and LPs bear the loss either way).

This is not a privileged-actor, relayer, or governance issue: it is a single unprivileged signed transaction crafted by an ordinary user, matching the "public underpriced work" / "wrong beneficiary or amount" pivot for value that must "conserve value and settle exactly once".

### Impact Explanation
Any asset-conversion pool configured as a fee asset for `ChargeAssetTxPayment` (this is the standard fee-payment configuration recommended for parachains like Asset Hub) is exposed to a self-sandwich: an attacker pays a smaller effective transaction fee (or extracts native/asset value from the pool on refund) by manipulating the very pool used to price their own fee refund, funded by other pool depositors. Repeated at scale this drains LP value and misprices a public dispatch wrapper's fee accounting — matching "theft or unbacked … value" and "wrong … amount" impact classes for live-scope `paritytech/polkadot-sdk` runtime code.

### Likelihood Explanation
High feasibility: no special permissions, keys, relayers, or governance actions are needed. Any account holding the fee asset and native asset (or enough of one to swap) can submit a single extrinsic whose `call` performs a large swap/liquidity change on the fee pool immediately before its own fee-refund conversion executes, all guaranteed atomic by construction of `TransactionExtension::pre_dispatch`/dispatch/`post_dispatch`. The precondition (a runtime enabling `SwapAssetAdapter` with `pallet-asset-conversion`) is an intended, documented configuration, not a misconfiguration.

### Recommendation
Do not re-quote the refund conversion against live reserves captured after the wrapped call executed. Either:
- Cache/lock the exchange rate obtained during `withdraw_fee` and use it (or a bounded/capped variant) for `correct_and_deposit_fee`'s refund swap instead of re-querying `quote_price_exact_tokens_for_tokens` on the post-dispatch state; or
- Explicitly block/limit the swap-back when the dispatched `call` itself touched the same pool (asset pair) used for fee payment; or
- Use a time-weighted/moving-average price source for the refund quote, consistent with the mitigation suggested in the source report.

### Proof of Concept
1. Configure a runtime with `pallet_asset_conversion_tx_payment::Config::OnChargeAssetTransaction = SwapAssetAdapter<Native, Fungibles, AssetConversion, ...>` and a pool `(Native, X)`.
2. Attacker submits a single signed extrinsic:
   - `tip`/fee asset = `X`
   - `call` = `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens` swapping a large amount of `X` into `Native` (or vice versa) inside the same `(Native, X)` pool, shifting reserves substantially.
3. Extension flow for this one extrinsic:
   - `withdraw_fee` (pre-dispatch, T0 reserves): swaps a small amount of `X` for `Native` to cover the estimated fee.
   - dispatch: attacker's own call executes, moving `(Native, X)` reserves to T1.
   - `correct_and_deposit_fee` (post-dispatch, T1 reserves): quotes/swaps the native refund back into `X` using the reserves the attacker just moved in step 2.
4. Because the refund-back conversion (step 3c) uses reserves manipulated by the attacker's own call in the same atomic transaction, the attacker can choose the direction/size of their pool swap in the `call` to make the refund conversion favorable to themselves at LPs' expense — with no requirement of a second transaction, other participants, or any privileged role. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-286)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1495-1547)
```rust
	#[pallet::view_functions]
	impl<T: Config> Pallet<T> {
		/// Returns the balance of each asset in the pool.
		/// The tuple result is in the order requested (not necessarily the same as pool order).
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

		/// Gets a quote for swapping an exact amount of `asset1` for `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L93-103)
```rust
/// Used to pass the initial payment info from pre- to post-dispatch.
#[derive(Encode, Decode, DefaultNoBound, TypeInfo)]
pub enum InitialPayment<T: Config> {
	/// No initial fee was paid.
	#[default]
	Nothing,
	/// The initial fee was paid in the native currency.
	Native(NativeLiquidityInfoOf<T>),
	/// The initial fee was paid in an asset.
	Asset((T::AssetId, AssetLiquidityInfoOf<T>)),
}
```
