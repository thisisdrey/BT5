## Analysis

The core broken invariant in the Zivoe report is: **a persistent accounting checkpoint (`basis`) is derived from an instantaneous, atomically-manipulable AMM spot price, and that checkpoint is later used (in a separate call) to settle real value — allowing an attacker to shift the AMM state, poison the checkpoint, and extract value on settlement.**

The closest local analog in this repo is `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which pays/refunds transaction fees paid in a non-native asset by reading the *live* `pallet-asset-conversion` pool reserves via `quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens` (a raw constant-product spot price with no TWAP or staleness protection), at two different points of the *same* extrinsic's lifecycle, with the dispatched call itself executing in between. [1](#0-0) 

### Title
Fee-refund uses post-dispatch AMM spot price, letting the dispatched call self-manipulate `pallet-asset-conversion` reserves to steal an inflated refund from the pool - (File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs)

### Summary
`SwapAssetAdapter::withdraw_fee` charges the fee-paying asset at pre-dispatch time using the pool's *current* reserves, while `SwapAssetAdapter::correct_and_deposit_fee` computes the unspent-weight refund using the pool's reserves as they stand *after* the wrapped call has already executed. Because the wrapped call is fully attacker-controlled and can itself invoke `pallet_asset_conversion::swap_exact_tokens_for_tokens` on the very same pool, a single extrinsic can shift the pool's reserve ratio between the withdrawal and the refund, causing the refund leg to be quoted and swapped at a self-manufactured, favorable rate and extracting value from the pool's liquidity providers.

### Finding Description
`ChargeAssetTxPayment::prepare` calls `withdraw_fee`, which — when the fee is paid in a non-native `asset_id` — quotes and withdraws the asset amount needed for the estimated `fee` using `S::quote_price_tokens_for_exact_tokens` against reserves `R0`: [2](#0-1) 

The wrapped `call` then dispatches with full user control, and can include `pallet_asset_conversion::Pallet::swap_exact_tokens_for_tokens` (or the `swap_tokens_for_exact_tokens` variant) on the same `asset_id`/native pool, moving its reserves from `R0` to an attacker-chosen `R1`. [3](#0-2) 

After the call executes, `post_dispatch_details` invokes `correct_and_deposit_fee`, which computes `refund_amount` (native) for unspent weight and converts it back into `asset_id` by quoting `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)` against the *now-manipulated* reserves `R1`, then executes the matching swap and deposits the result to the user: [4](#0-3) 

The `QuotePrice` trait itself documents that its guarantee only holds "if no other swaps are made after the price is quoted and before the target swap ... within the same transaction" — but here the exposure is not between quote-and-swap (those are adjacent and consistent), it is between the fee **withdrawal** (`R0`) and the fee **refund** (`R1`), which straddles the arbitrary dispatched call: [1](#0-0) 

By swapping native into `asset_id` inside the wrapped call (shrinking `reserve_native`, growing `reserve_asset_id`), the attacker makes the reverse conversion (native→`asset_id`, used for the refund) yield more `asset_id` per unit of native than it would have at `R0`. Since `refund_asset_amount` is deposited to `who` in full via `F::resolve`, that excess is extracted from the pool's liquidity (funded by LPs), not from the attacker's own capital beyond swap fees.

### Impact Explanation
This lets an unprivileged, single-signer transaction extract real value from a shared AMM pool (liquidity providers' funds) by manipulating the exact price oracle used to settle the fee refund, with no reliance on a malicious relayer, validator, governance actor, or front-running by a third party — the manipulation and the exploitation both occur inside the attacker's own extrinsic. This falls under "theft ... or unbacked mint or unlock" / conservation-of-value guarantees for pool/contract-held value that this SDK's guidance calls out as in-scope.

### Likelihood Explanation
Exploitability is bounded by economics: the manipulation swap pays the pool's LP fee and the refund itself is typically small (only the unspent-weight portion of the fee), so profitability requires either a shallow pool relative to the attacker's capital or a call whose declared weight vastly exceeds its actual consumed weight (maximizing `refund_amount`) combined with cheap manipulation. This is analogous to the Sherlock finding, which was rated Medium precisely because the attack is possible but has cost/probability caveats — the same "possible, low-but-nonzero-probability, protocol accounting distortion" profile applies here.

### Recommendation
- Snapshot the pool reserves (or use a TWAP / price bound) at `withdraw_fee` time and reuse that snapshot (or bound the refund quote to it with a tolerance) in `correct_and_deposit_fee`, rather than re-querying live spot reserves after the wrapped call has executed.
- Alternatively, cap the refund's implied price to the price observed at withdrawal (e.g., refund at min(price_at_withdraw, price_at_refund)) so the wrapped call cannot create a profitable withdraw/refund price gap.
- Add a regression test that dispatches a call which itself swaps against the same fee-payment pool and asserts the pool's post-extrinsic balance is not reduced beyond LP-fee-consistent bounds.

### Proof of Concept
1. Attacker holds asset `X` (a `pallet-asset-conversion` pool exists for `X`/native) and some native tokens.
2. Attacker submits an extrinsic with `ChargeAssetTxPayment { asset_id: Some(X), .. }` wrapping a `call` that:
   a. First calls `pallet_asset_conversion::swap_exact_tokens_for_tokens` swapping a large amount of native for `X` on the same pool, pushing `reserve_native` down and `reserve_X` up.
   b. Declares a large `weight` in `DispatchInfo` but performs cheap/no-op work so `post_info` reports little weight consumed (maximizing `unspent_weight`/`refund_amount`).
3. `withdraw_fee` (pre-dispatch, at `R0`) withdraws `X` from attacker for the estimated fee.
4. The wrapped call executes step (a), moving reserves to `R1`.
5. `post_dispatch_details` → `correct_and_deposit_fee` quotes and swaps `refund_amount` (native) → `X` at `R1`, depositing the resulting `X` (inflated relative to `R0`-based pricing) to the attacker, sourced from pool liquidity.
6. Net effect: the pool's LPs bear a value transfer that would not occur if the refund had been priced consistently with the fee withdrawal, mirroring the Zivoe `basis`-reset exploit where a manipulated AMM read corrupts a value used in a later, separate settlement step. [2](#0-1) [5](#0-4)

### Citations

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-322)
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
			// The error should not occur since swap was quoted before.
			Err((refund, _)) => {
				defensive!(
					"Refund swap should pass for the quoted amount",
					(refund.asset(), refund.peek(), refund_asset_amount, who)
				);
				// cancel `refund` and include it back into `adjusted_paid`.
				adjusted_paid.merge(refund).map_or_else(
					|(adjusted_paid, refund)| {
						defensive!(
							"`adjusted_paid` and `refund` are credits of the same asset.",
							(adjusted_paid.asset(), refund.asset(), who)
						);
						// drop `refund` and return `adjusted_paid` without it.
						(fee_asset_amount, adjusted_paid)
					},
					|fee_paid| (fee_asset_amount, fee_paid),
				)
			},
		};

		// Handle the imbalance (fee and tip separately).
		let (tip, fee) = adjusted_paid.split(tip);
		OU::on_unbalanceds(Some(fee).into_iter().chain(Some(tip)));
		return Ok(fee_asset_amount);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L968-1004)
```rust
		/// Swap exactly `amount_in` of asset `path[0]` for asset `path[1]`.
		/// If an `amount_out_min` is specified, it will return an error if it is unable to acquire
		/// the amount desired.
		///
		/// Withdraws the `path[0]` asset from `sender`, deposits the `path[1]` asset to `send_to`,
		/// respecting `keep_alive`.
		///
		/// If successful, returns the amount of `path[1]` acquired for the `amount_in`.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		pub(crate) fn do_swap_exact_tokens_for_tokens(
			sender: T::AccountId,
			path: Vec<T::AssetKind>,
			amount_in: T::Balance,
			amount_out_min: Option<T::Balance>,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> Result<T::Balance, DispatchError> {
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

			Self::swap(&sender, &path, &send_to, keep_alive)?;
```
