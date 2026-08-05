### Title
`ChargeAssetTxPayment` withdraws non-native fee assets using an unbounded on-chain AMM quote, exposing every asset-fee-paying user to sandwich attacks - ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
The `SwapAssetAdapter` implementation of `OnChargeAssetTransaction`, used by `pallet-asset-conversion-tx-payment`, converts a user's chosen fee asset into the native fee asset by calling `quote_price_tokens_for_exact_tokens` and then immediately consuming that exact quote via `swap_tokens_for_exact_tokens`/`swap_exact_tokens_for_tokens`. Unlike the pallet's own extrinsics (`swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`), which take a user-supplied `amount_out_min`/`amount_in_max`, the transaction extension `ChargeAssetTxPayment` exposes no slippage parameter at all — `asset_id` and `tip` are the only fields [1](#0-0) . This mirrors exactly the flagged pattern in the external report: using an on-chain quote as if it were a trustworthy price, with no attacker-independent bound supplied by the party bearing the risk.

### Finding Description
`withdraw_fee` computes `asset_fee` on-chain from the current AMM reserves via `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)`, withdraws exactly that amount from the user, and swaps it for the exact native `fee` amount: [2](#0-1) 

Likewise, `correct_and_deposit_fee` quotes and swaps the refund leg the same way: [3](#0-2) 

This is invoked automatically for *every signed extrinsic* whose sender selects a non-native `asset_id` in `ChargeAssetTxPayment` — a fully public, permissionless, unprivileged code path with no admin/governance/relayer involvement [4](#0-3) .

The broken invariant is the same one identified in the external report: an on-chain-computed AMM quote is used as the sole "slippage protection," with the amount actually charged to the user derived from whatever the pool's reserves are *at the moment the block includes this transaction* — a value trivially manipulable by anyone who can place a transaction earlier in the same block (a transaction the attacker constructs and submits, no relayer/validator/collateral compromise required). Because the extension gives the fee-payer zero ability to bound the maximum amount of `asset_id` they are willing to pay (there is no `max_asset_fee`/`amount_in_max` field on `ChargeAssetTxPayment`), an attacker can:
1. Submit tx A immediately before the victim's transaction in the same block, pushing the `asset_id`/native pool price so that quoting `fee` native tokens costs far more `asset_id` than fair value.
2. The victim's `withdraw_fee` (running inside transaction validation/pre-dispatch for the victim's tx, sequenced right after tx A) quotes and immediately swaps at the manipulated price, extracting an inflated amount of `asset_id` from the victim.
3. Submit tx B immediately after to reverse the price manipulation and realize the arbitrage profit, leaving the victim's overpaid `asset_id` value with the pool/attacker.

The pallet's own AMM extrinsics prevent exactly this by requiring callers to supply `amount_out_min`/`amount_in_max`, enforced in `do_swap_exact_tokens_for_tokens`/`do_swap_tokens_for_exact_tokens` [5](#0-4) [6](#0-5) . `SwapAssetAdapter` bypasses this guard entirely for fee payment: it self-selects a "min"/"max" equal to the just-computed quote and expects it to always match at execution time, `defensive!`-asserting if it doesn't [7](#0-6) . That assertion only guards against internal inconsistency between the quote and swap calls made back-to-back in the same function; it does nothing to protect against the price having already been manipulated before the quote is even taken.

### Impact Explanation
Every account that opts to pay fees in a non-native asset (a normal, everyday, unprivileged action) can have its effective fee inflated arbitrarily by an attacker who sandwiches the fee-conversion swap, extracting value at the victim's expense with no cap. This is a direct "theft"/fund-loss vector against ordinary users through a public dispatch-adjacent path (a `TransactionExtension` executed for any signed extrinsic), fitting the "public underpriced work" / "unbacked value transfer to wrong beneficiary/amount" impact class in scope. Any parachain or Substrate runtime that wires up `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` (e.g. the reference `substrate/bin/node/runtime` and the Penpal testing parachain) is affected [8](#0-7) [9](#0-8) .

### Likelihood Explanation
Medium-to-High: the attack requires only the ability to submit ordinary transactions ahead of and behind a target victim transaction within the same block — a standard, well-known sandwich pattern requiring no validator/collator/relayer collusion, no leaked keys, and no governance action. Any pool with meaningful non-native-fee-asset usage and modest liquidity depth is exploitable whenever price impact from a moderate-size manipulation trade exceeds gas/opportunity cost.

### Recommendation
Add a caller-supplied maximum acceptable `asset_id` fee (analogous to `amount_in_max`) to `ChargeAssetTxPayment`/`OnChargeAssetTransaction::withdraw_fee`, and reject the transaction (or fail validation) if the quoted `asset_fee` exceeds it, mirroring the slippage guard already present in `do_swap_tokens_for_exact_tokens`. Similarly bound the refund leg in `correct_and_deposit_fee` with a minimum acceptable refund. Consider also using a TWAP/oracle-resistant price source rather than the raw spot reserves for fee conversion.

### Proof of Concept
Conceptual sequence (requires no privileged role):
1. Attacker observes a pending victim transaction that sets `ChargeAssetTxPayment { asset_id: Some(X), .. }` for a fee-conversion pool `(X, Native)`.
2. Attacker submits tx A with higher priority/tip to land immediately before the victim's transaction, performing a large `swap_exact_tokens_for_tokens` that skews the `X`/`Native` pool ratio so that `Native` becomes expensive in terms of `X`.
3. Victim's transaction executes; `ChargeAssetTxPayment::withdraw_fee` → `SwapAssetAdapter::withdraw_fee` quotes `asset_fee = quote_price_tokens_for_exact_tokens(X, Native, fee, true)` against the now-skewed pool and withdraws that (inflated) amount of `X` from the victim, with no cap the victim can set.
4. Attacker submits tx B immediately after, reversing the pool skew and capturing the spread as profit — the victim has paid substantially more `X` for the same native `fee` than they would have absent manipulation, and has no recourse since the extension never let them specify a maximum. [2](#0-1)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L176-182)
```rust
#[derive(Encode, Decode, DecodeWithMemTracking, Clone, Eq, PartialEq, TypeInfo)]
#[scale_info(skip_type_params(T))]
pub struct ChargeAssetTxPayment<T: Config> {
	#[codec(compact)]
	tip: BalanceOf<T>,
	asset_id: Option<T::AssetId>,
}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs (L193-219)
```rust
	/// Fee withdrawal logic that dispatches to either [`Config::OnChargeAssetTransaction`] or
	/// [`pallet_transaction_payment::Config::OnChargeTransaction`].
	fn withdraw_fee(
		&self,
		who: &T::AccountId,
		call: &T::RuntimeCall,
		info: &DispatchInfoOf<T::RuntimeCall>,
		fee: BalanceOf<T>,
	) -> Result<(BalanceOf<T>, InitialPayment<T>), TransactionValidityError> {
		debug_assert!(self.tip <= fee, "tip should be included in the computed fee");
		if fee.is_zero() {
			Ok((fee, InitialPayment::Nothing))
		} else if let Some(asset_id) = &self.asset_id {
			T::OnChargeAssetTransaction::withdraw_fee(
				who,
				call,
				info,
				asset_id.clone(),
				fee,
				self.tip,
			)
			.map(|payment| (fee, InitialPayment::Asset((asset_id.clone(), payment))))
		} else {
			T::OnChargeTransaction::withdraw_fee(who, call, info, fee, self.tip)
				.map(|payment| (fee, InitialPayment::Native(payment)))
		}
	}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-176)
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-300)
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
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1036-1050)
```rust
			ensure!(amount_out > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_in_max) = amount_in_max {
				ensure!(amount_in_max > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_out(amount_out, path)?;

			let amount_in = path.first().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_in_max) = amount_in_max {
				ensure!(
					amount_in <= amount_in_max,
					Error::<T>::ProvidedMaximumNotSufficientForSwap
				);
			}
```

**File:** substrate/bin/node/runtime/src/lib.rs (L683-695)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = NativeOrWithId<u32>;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		Native,
		NativeAndAssets,
		AssetConversion,
		ResolveAssetTo<TreasuryAccount, NativeAndAssets>,
	>;
	type WeightInfo = pallet_asset_conversion_tx_payment::weights::SubstrateWeight<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
}
```

**File:** cumulus/parachains/runtimes/testing/penpal/src/lib.rs (L754-767)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = Location;
	type OnChargeAssetTransaction = pallet_asset_conversion_tx_payment::SwapAssetAdapter<
		xcm_config::PenpalNativeCurrency,
		NativeAndAssets,
		AssetConversion,
		MaybeResolveAssetTo<BlockAuthor<Runtime>, NativeAndAssets, AccountId>,
	>;
	type WeightInfo = ();

	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetTxConversionHelper;
}
```
