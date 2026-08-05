Audit Report

## Title
Transaction-fee asset swap trusts unprotected instantaneous AMM spot price, enabling underpriced-fee spam via same-block pool manipulation - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

## Summary
`SwapAssetAdapter::withdraw_fee` determines the non-native fee-asset amount a signer must pay by calling `quote_price_tokens_for_exact_tokens`, which reads the pool's live reserves with no time-weighted averaging, minimum-elapsed-block requirement, or manipulation guard. [1](#0-0)  Because pool reserves can be skewed by an ordinary `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` call in a preceding extrinsic within the same block, an attacker can cause the quoted `asset_fee` to be artificially small relative to its true economic value, letting weight/length-heavy transactions be paid for at below-intended cost.

## Finding Description
`quote_price_tokens_for_exact_tokens` computes the requested amount directly from `Self::get_reserves`, i.e., the pool account's current balances, with no staleness or freshness check. [2](#0-1)  The pallet's own documentation acknowledges "the price may have changed by the time the transaction is executed," a warning intended for slippage-protected user swaps (which take `amount_out_min`/`amount_in_max`) — but `withdraw_fee` reuses the identical unprotected quote for fee determination, where no slippage bound applies at all. [3](#0-2) 

In `withdraw_fee`, the code quotes `asset_fee` from the live pool state and then immediately withdraws and swaps exactly that quoted amount via `swap_tokens_for_exact_tokens`, with no check against a reference/TWAP price or a bound on how recently the pool's reserves changed. [4](#0-3)  An attacker can submit a prior extrinsic in the same block that shifts the pool's reserve ratio (e.g., `do_swap_exact_tokens_for_tokens`, which updates reserves immediately with no cooldown), [5](#0-4)  then submit the target heavy extrinsic with `ChargeAssetTxPayment` referencing the skewed pool, causing `withdraw_fee` to quote and withdraw a smaller-than-intended `asset_fee`. This is wired into production: Asset Hub Westend configures `SwapAssetAdapter` as `OnChargeAssetTransaction`, [6](#0-5)  as does the kitchensink node runtime. [7](#0-6) 

However, this manipulation is not free: any reserve-skewing swap on a constant-product AMM incurs the pool's LP fee on the trade notional, and reversing the skew (to recover capital) incurs the LP fee again plus price-impact slippage that scales with the size of the skew. The net "savings" available to the attacker on the underpriced fee is bounded above by the fee amount `F` itself (a small, fixed value — a transaction fee), while the cost of producing a given price shift scales with the notional traded relative to pool depth. For this to be net profitable, the pool must be so shallow that a very small, low-cost trade produces a large relative price shift; even then, the manipulating swap itself must be submitted and paid for (at normal fee rates) as a separate extrinsic, consuming its own blockspace. This bounds attacker profit and required manipulation cost tightly to pool-specific liquidity conditions rather than presenting an unconditionally exploitable drain.

## Impact Explanation
This maps to "public underpriced work that degrades block production": an unprivileged signed account holding a modest position in a thin fee-asset pool can pay less than the intended native-equivalent fee for a heavy extrinsic while still consuming full chain weight/blockspace, weakening the spam-resistance guarantee that `pallet_transaction_payment`'s fee mechanism is meant to provide. The magnitude of the underpayment achievable per transaction is bounded by the fee amount itself and by AMM round-trip costs (LP fees + slippage), which limits severity to pools with unusually shallow liquidity relative to the fees being paid.

## Likelihood Explanation
No privileged role is required — only two ordinary signed extrinsics (a reserve-skewing swap and the target fee-underpaying transaction) submitted within the same block. Exploitability is inversely proportional to the targeted fee-asset pool's liquidity depth; since any account can create a pool via `create_pool`, thin, newly-created pools configured for fee payment are the primary risk surface. Because manipulation cost (LP fee + slippage on the round trip) scales with pool depth while benefit is capped at the fixed fee amount `F`, the attack is only consistently profitable against unusually shallow pools.

## Recommendation
`SwapAssetAdapter::withdraw_fee` should not rely on the raw instantaneous quote for fee-critical accounting. Options include: (a) bounding the deviation between the quoted price and a longer-window/TWAP-style reference price before accepting it for fee payment, (b) requiring pools used for fee payment to satisfy a minimum-liquidity/maximum price-impact threshold, or (c) requiring a minimum number of elapsed blocks since the pool's last reserve-changing operation before its price is trusted for fee determination.

## Proof of Concept
1. Fund a shallow pool `P = (Native, AssetX)` with thin `AssetX` liquidity (e.g., reserves `(1_000_000, 200)`) via `AssetConversion::add_liquidity`.
2. Attacker submits `AssetConversion::swap_exact_tokens_for_tokens` sending Native into `P`, shifting `AssetX`'s marginal price relative to Native (per `do_swap_exact_tokens_for_tokens` at `substrate/frame/asset-conversion/src/lib.rs:980-1014`).
3. In the same block, attacker submits a weight-heavy extrinsic with `ChargeAssetTxPayment { asset_id: Some(AssetX), .. }`; `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(AssetX, Native, fee, true)` against the skewed reserves (`payment.rs:142-146`), returning a smaller `asset_fee` than under unskewed reserves.
4. Optionally submit a reverse swap to reclaim capital, net cost limited to the pool's LP fee and slippage.
5. Compare the `asset_fee` actually withdrawn (via the `AssetTxFeePaid` event, see `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs:293-298` for the event shape) against the fee that would have been quoted from unskewed reserves to quantify the discount achieved.

### Citations

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L148-173)
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

		// Since the exact price for `fee` has been quoted, the change should be zero.
		ensure!(change.peek().is_zero(), InvalidTransaction::Payment);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L980-1014)
```rust
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

			Self::deposit_event(Event::SwapExecuted {
				who: sender,
				send_to,
				amount_in,
				amount_out,
				path,
			});
			Ok(amount_out)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1547)
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
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs (L1176-1188)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = xcm::v5::Location;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		WestendLocation,
		NativeAndNonPoolAssets,
		AssetConversion,
		ResolveAssetTo<StakingPot, NativeAndNonPoolAssets>,
	>;
	type WeightInfo = weights::pallet_asset_conversion_tx_payment::WeightInfo<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
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
