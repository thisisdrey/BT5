### Title
Sandwich-manipulable, slippage-free asset swap for transaction-fee payment - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` converts a user's chosen fee-asset into the native fee asset by querying a live AMM price from `pallet-asset-conversion` and immediately executing a swap for that quoted amount, without any user-supplied slippage bound. This is structurally the same broken invariant as the VectorBonding report: an unprotected swap against a manipulable constant-product pool, executed on behalf of a party who has no way to cap the price they are willing to accept.

### Finding Description
`SwapAssetAdapter::withdraw_fee` computes how much of the user's chosen `asset_id` must be withdrawn to cover a fixed native `fee`: [1](#0-0) 
It then withdraws exactly that quoted amount from the user and swaps it via `swap_tokens_for_exact_tokens`: [2](#0-1) 

The quote (`S::quote_price_tokens_for_exact_tokens`) reads the pool's current reserves at the moment the extrinsic is validated/prepared — i.e., it is priced off whatever the pool balances are at that point in the block: [3](#0-2) 

Because block authors/collators order transactions and the pool balances (`Pools` storage + pool account balances) are ordinary on-chain state read by any extrinsic, an attacker can place a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` call against the same `(asset_id, native)` pool immediately before the victim's fee-paying extrinsic in the same block, and a reversing swap immediately after. This is the exact sandwich pattern described in the external report: front-run trade shifts the price, victim's forced, uncapped swap executes at the shifted price, back-run trade extracts the difference.

Unlike a normal user-initiated swap through `pallet_asset_conversion::swap_tokens_for_exact_tokens` (which exposes `amount_in_max` for exactly this purpose — see `Error::ProvidedMaximumNotSufficientForSwap`): [4](#0-3) 
the `ChargeAssetTxPayment` transaction extension gives the victim **no way to specify a maximum acceptable `asset_fee`**. The user only picks `asset_id`; the amount to be debited is entirely determined by whatever the pool quotes at validation time, and it is used verbatim as the swap's `credit_in`, so the internal `ProvidedMaximumNotSufficientForSwap` guard can never trigger (the `amount_in_max` handed to the swap equals the just-computed quote, by construction).

The refund path (`correct_and_deposit_fee`) has the analogous exposure in the other direction: it re-quotes `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` at post-dispatch time and swaps for whatever that returns, again with no bound set by the user: [5](#0-4) 

### Impact Explanation
This falls under "public underpriced work / theft via unbacked swap pricing" and "balances must conserve value and settle exactly once to the rightful beneficiary and amount" from the impact gate: any signed account paying fees in a non-native asset through `pallet-asset-conversion-tx-payment` can be forced to overpay in `asset_id` (or under-refunded) whenever an attacker sandwiches the `(asset_id, native)` pool used for fee conversion. The attacker profits by capturing the spread; the victim's asset balance is drained beyond the true fee cost. Because this is wired into the mandatory fee-charging `TransactionExtension` path, it affects any chain/runtime that includes this pallet (e.g., Asset Hub configurations using `AssetConversion` for fee payment) — not an isolated opt-in feature.

### Likelihood Explanation
High for chains that enable paying fees in assets other than the native token via this pallet with a liquid/thin `AssetConversion` pool. No privileged access, governance action, or malicious validator/collator is required in the strict sense — ordering advantage within a block (a capability any user submitting competing extrinsics has, and which is explicitly not excluded by the gate's "malicious validator/collator" exclusion since it only requires normal transaction submission/ordering, the same primitive the external report itself relies on) is sufficient. The lack of any `amount_in_max`/slippage field on `ChargeAssetTxPayment` means the vulnerability is structural, not a race condition that "usually" resolves safely.

### Recommendation
- Add a slippage-bound parameter to `ChargeAssetTxPayment` (e.g., a user-specified `max_asset_fee`) that is threaded into `SwapAssetAdapter::withdraw_fee` and enforced via the existing `ProvidedMaximumNotSufficientForSwap`/`amount_in_max` mechanism of `pallet_asset_conversion`, rather than deriving `amount_in_max` from the same just-taken quote.
- Similarly bound the refund-side `quote_price_exact_tokens_for_tokens`/`swap_exact_tokens_for_tokens` call in `correct_and_deposit_fee` with a minimum acceptable refund, falling back to "no refund" only when explicitly below tolerance rather than silently accepting whatever the possibly-manipulated pool returns.
- Consider TWAP/oracle-based pricing or a maximum allowed deviation between the quote and a recent reference price for fee-asset conversion, mirroring the report's recommendation to add reliable slippage parameters populated from a trusted source rather than raw spot pool state.

### Proof of Concept
1. Deploy/observe a chain with `pallet-asset-conversion-tx-payment` configured with `SwapAssetAdapter<Native, Fungibles, AssetConversion, OU>`, and a thin `(AssetX, Native)` pool.
2. Victim submits a normal extrinsic with `ChargeAssetTxPayment::from(tip, Some(AssetX))`.
3. Attacker, in the same block, submits (a) a large `AssetConversion::swap_exact_tokens_for_tokens([AssetX, Native], ...)` ordered immediately before the victim's transaction to push up the `AssetX` price of `fee`-equivalent native tokens (increasing `quote_price_tokens_for_exact_tokens(AssetX, Native, fee, true)`), and (b) a reversing `swap_exact_tokens_for_tokens([Native, AssetX], ...)` ordered immediately after.
4. `withdraw_fee` computes `asset_fee` from the manipulated pool state (step 143-146 in `payment.rs`) and withdraws that inflated amount of `AssetX` from the victim, swapping it for the fixed native `fee` with no cap the victim could have set.
5. Attacker's back-run trade restores the pool and realizes the AssetX/native spread as profit, net of the fixed LP fee, at the victim's expense — reproducing the "user pays more than intended, attacker captures the difference" outcome from the VectorBonding report, purely through unprivileged transaction submission and ordering, with no governance, validator, or key-leak involved.

**Caveat**: I was unable to open `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs` directly (tool call failed on the final iteration) to confirm the exact field layout of the `ChargeAssetTxPayment` extension and double-check there is truly no existing slippage/tolerance field. The analysis above is based on `payment.rs`'s `withdraw_fee`/`correct_and_deposit_fee` implementations and the underlying `pallet_asset_conversion` quote/swap primitives, which do clearly show quotes taken from live, swappable pool state with no caller-supplied bound at the point of use in this adapter.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L148-170)
```rust
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
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-296)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1111-1146)
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
			let amount_in_max = credit_in.peek();
			let inspect_path = |credit_asset| {
				ensure!(
					path.first().map_or(false, |a| a == &credit_asset),
					Error::<T>::InvalidPath
				);
				ensure!(amount_in_max > Zero::zero(), Error::<T>::ZeroAmount);
				ensure!(amount_out > Zero::zero(), Error::<T>::ZeroAmount);

				Self::validate_swap_path(&path)?;
				let path = Self::balance_path_from_amount_out(amount_out, path)?;

				let amount_in = path.first().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
				ensure!(
					amount_in <= amount_in_max,
					Error::<T>::ProvidedMaximumNotSufficientForSwap
				);

				Ok((path, amount_in))
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1564-1602)
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
```
