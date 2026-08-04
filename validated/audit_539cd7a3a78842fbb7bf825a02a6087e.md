## Analysis

The report's core broken invariant: a protocol-controlled operation derives a critical monetary amount from **live AMM spot reserves** (`getAmountsOut`) and then executes value transfer against that unverified spot price, with the only "protection" being an internally-fixed slippage parameter the caller cannot meaningfully control. A local analog with identical mechanics exists in `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`.

### Where the same primitive appears

In `withdraw_fee`, when a signed extrinsic opts to pay fees in a non-native asset, the pallet quotes the fee entirely from the current `pallet-asset-conversion` pool reserves and then immediately executes the swap against that same quote: [1](#0-0) 

The refund/correction path does the same thing again for the reverse direction: [2](#0-1) 

Both quotes are backed by `pallet_asset_conversion::Pallet::quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens`, which read the pool's live `get_reserves()` and compute price via the constant-product formula — the on-chain equivalent of `UNISWAP_V2_ROUTER_02.getAmountsOut`: [3](#0-2) [4](#0-3) 

The pallet's own documentation acknowledges the spot-price risk but only tells *callers of the dispatchable swap* to use `amount_out_min`/`amount_in_max` — the transaction-fee pallet, acting as an internal "protocol" consumer of the quote, has no such external slippage input at all; it trusts the instantaneous quote unconditionally: [5](#0-4) 

### Why this reproduces the report's exact flaw

- The quote and swap are only atomic *within* `withdraw_fee`'s own execution, but the reserves it reads reflect the state left by **every prior extrinsic in the same block**, including extrinsics submitted by the same unprivileged account. `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` on `pallet-asset-conversion` are ordinary signed dispatchables anyone can call to move the pool's reserves before their own fee-paying extrinsic is processed: [6](#0-5) 
- Because the fee-in-asset amount is derived purely from that manipulable spot price, and the pallet enforces no independent floor/ceiling beyond the caller's own manipulated pool state, an attacker can skew the `asset_id`/native reserves with one extrinsic, have their fee-paying extrinsic charged an artificially small amount of `asset_id` (or artificially large refund) via `quote_price_tokens_for_exact_tokens`, then restore the pool with a follow-up swap — capturing the AMM fee/slippage delta while causing the chain to under-collect real economic value for the weight/length actually consumed. This is precisely the "public underpriced work that degrades block production" impact class: transaction fees exist to price block space, and this path lets that price be manipulated with ordinary, permissionless transactions, not a malicious validator/collator/relayer.
- No slippage parameter is exposed on the `ChargeAssetTxPayment` extension itself (`tip`, `asset_id` only) for the caller to bound the swap, mirroring the report's "protocol inputs slippagePercent instead of amount out" weakness: [7](#0-6) 

### Title
Fee-in-asset quoting in `SwapAssetAdapter` trusts manipulable AMM spot reserves with no caller-supplied slippage bound - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` and `correct_and_deposit_fee` price non-native transaction fees purely from `pallet_asset_conversion`'s instantaneous pool reserves via `quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens`, then execute the swap against that same quote. Any unprivileged account can move those reserves with an ordinary prior extrinsic in the same block, distorting how much value the chain actually collects for the weight/length of the fee-paying extrinsic.

### Finding Description
`quote_price_tokens_for_exact_tokens`/`quote_price_exact_tokens_for_tokens` compute price directly from `get_reserves()` (constant-product AMM formula), which reflects whatever state prior extrinsics in the block left behind. `SwapAssetAdapter::withdraw_fee` uses this quote to determine `asset_fee` — the amount of the user-chosen asset withdrawn — with no independent, caller-supplied bound; the extension type `ChargeAssetTxPayment` carries only `tip` and `asset_id`, no min/max. `correct_and_deposit_fee` repeats the pattern for refunds.

### Impact Explanation
Fees are the chain's mechanism for pricing block space/weight against spam and DoS. If the fee-in-asset amount can be pushed arbitrarily low relative to its real value by manipulating the reserves it's quoted from, an attacker can pay for transaction weight/length with a manipulated/underpriced amount of the chosen asset, systematically underpricing block production work — matching the "public underpriced work that degrades block production" impact class.

### Likelihood Explanation
Medium: requires only ordinary signed extrinsics (a swap to skew the pool immediately before/after the fee-paying transaction), no privileged role, malicious validator, or off-chain infrastructure — any account controlling both the pool-skewing swap and the fee-paying extrinsic (or paying attention to shallow pools) can exploit this.

### Recommendation
Require the transaction-fee-in-asset path to use a time-weighted or otherwise manipulation-resistant price source, or expose an explicit caller-supplied maximum asset amount (analogous to `amount_in_max`) for `ChargeAssetTxPayment`, and reject transactions if the spot quote deviates from that bound rather than trusting the pool's current reserves unconditionally.

### Proof of Concept
1. Deploy/observe a shallow `pallet-asset-conversion` pool for `asset_id`/native `A`.
2. Attacker submits extrinsic 1: a large `swap_exact_tokens_for_tokens` skewing the pool reserves in their favor.
3. Attacker (or their following extrinsic in the same block) submits a transaction using `ChargeAssetTxPayment { asset_id: Some(asset_id), .. }`; `withdraw_fee` calls `quote_price_tokens_for_exact_tokens` against the now-skewed reserves, producing an `asset_fee` far below fair value for the native `fee`.
4. Attacker submits extrinsic 3 reversing the swap from step 2, restoring the pool and collecting the AMM-side arbitrage, while the chain has settled the extrinsic's real weight/length cost for an economically underpriced amount of `asset_id`. [8](#0-7)

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-277)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L525-545)
```rust
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::swap_exact_tokens_for_tokens(path.len() as u32))]
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1562)
```rust
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

			// Small inputs can round output to zero due to integer division.
			if amount_out.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output = T::Assets::reducible_balance(asset2, &pool_account, Preserve, Polite);
			if amount_out > max_output {
				return None;
			}

			Some(amount_out)
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1616-1630)
```rust
		///
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_in_max` to control slippage.)
		fn quote_price_tokens_for_exact_tokens(
			asset1: AssetId,
			asset2: AssetId,
			amount: Balance,
			include_fee: bool,
		) -> Option<Balance>;

		/// Provides a quote for [`Pallet::swap_exact_tokens_for_tokens`].
		///
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
		fn quote_price_exact_tokens_for_tokens(
```

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
