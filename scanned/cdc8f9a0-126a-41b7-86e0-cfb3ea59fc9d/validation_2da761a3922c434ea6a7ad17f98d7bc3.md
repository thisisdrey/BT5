Confirmed: `SwapAssetAdapter` is wired into `pallet_asset_conversion_tx_payment::Config::OnChargeAssetTransaction` in production runtimes including Asset Hub Westend/Rococo, the kitchensink node runtime, and the staking-async parachain runtime.

### Title
Transaction-fee asset swap trusts unprotected instantaneous AMM spot price, enabling underpriced-fee spam via same-block pool manipulation - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` determines how much of a non-native fee-asset a signer must pay by calling `pallet_asset_conversion`'s `quote_price_tokens_for_exact_tokens`, which reads the AMM pool's current reserves with no time-weighted averaging, minimum-elapsed-block requirement, or manipulation guard [1](#0-0) . This mirrors the reported `VeloOracle.getOraclePrice` flaw: both treat an instantaneous, single-observation AMM price as safe-to-use without checking whether it could have been freshly manipulated.

### Finding Description
`quote_price_tokens_for_exact_tokens` and `quote_price_exact_tokens_for_tokens` compute pricing directly from `get_reserves`, i.e., the pool account's current balances, with no staleness or freshness check comparable to a TWAP or minimum block-delay requirement [2](#0-1) . The pallet's own documentation even warns "the price may have changed by the time the transaction is executed" [3](#0-2) , acknowledging the spot price is trivially manipulable — this warning is intended for slippage-protected user swaps (which take `amount_out_min`/`amount_in_max`), but `SwapAssetAdapter` reuses the exact same unprotected quote for **fee determination**, where there is no user-supplied slippage bound at all.

In `withdraw_fee`, when the signer chooses to pay fees in a non-native asset, the code:
1. Calls `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)` against the live pool reserves to determine `asset_fee` [1](#0-0) .
2. Immediately withdraws and swaps exactly that quoted amount [4](#0-3) .

Because the quote and swap occur in the same extrinsic application, there's no intra-extrinsic arbitrage — but the *pool reserves themselves* are attacker-controlled state that can be skewed by a prior extrinsic in the same block via `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`, which update reserves immediately with no cooldown [5](#0-4) . This is wired into real chains: Asset Hub Westend, Asset Hub Rococo, the staking-async parachain runtime, and the kitchensink node runtime all configure `SwapAssetAdapter` as `OnChargeAssetTransaction` [6](#0-5) [7](#0-6) .

### Impact Explanation
An attacker who holds a modest amount of a thinly-liquid fee-asset can:
1. Submit a swap extrinsic that skews the fee-asset/native pool's reserve ratio in their favor (temporarily making the fee-asset "cheap" relative to native from the AMM's perspective).
2. Immediately submit their real (weight/length-heavy) extrinsic paying fees in that fee-asset; `withdraw_fee` quotes and withdraws only the artificially small `asset_fee` amount for the skewed pool state.
3. Optionally reverse the initial swap afterward to reclaim most of the capital, at the cost of the pool's swap fee only.

This lets the attacker pay a real transaction fee (native-asset-equivalent economic value) far below the fee schedule's intended value while still consuming full chain weight/blockspace, i.e., "public underpriced work that degrades block production." Because `pallet_transaction_payment`'s fee mechanism exists specifically to make weight/length consumption costly enough to deter spam, this bypass allows cheap, repeated congestion of a parachain (e.g., Asset Hub) without needing a malicious validator, collator, or governance actor — any ordinary signed account can do it.

### Likelihood Explanation
Requires no privileged role — only two ordinary signed extrinsics (a skewing swap and the underpriced-fee transaction) submitted by the same or coordinated accounts within one block. Exploitability scales inversely with the fee-asset pool's liquidity depth relative to the fee amount; smaller/newer pools (which are common, since any account can create a pool) are most exposed.

### Recommendation
`SwapAssetAdapter::withdraw_fee` should not rely on the raw instantaneous quote for fee-critical accounting. Options: (a) bound the deviation between the quoted price and a longer-window/TWAP-style reference price before accepting it for fee payment, (b) require pools used for fee payment to satisfy a minimum-liquidity/maximum price-impact threshold, or (c) require a minimum number of elapsed blocks since the pool's last reserve-changing operation before its price can be used to underwrite fee payment, analogous to the Velodrome fix.

### Proof of Concept
1. Fund a small pool `P = (Native, AssetX)` with shallow AssetX liquidity, e.g., reserves `(1_000_000, 200)`.
2. Attacker (holding AssetX and Native) submits `AssetConversion::swap_exact_tokens_for_tokens` sending a large amount of Native into `P`, sharply reducing AssetX's price relative to Native.
3. In the same block, attacker submits any weight-heavy extrinsic with `ChargeAssetTxPayment { asset_id: Some(AssetX), .. }`. `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(AssetX, Native, fee, true)` against the now-skewed reserves [1](#0-0) , returning a much smaller `asset_fee` than it would against the pool's normal reserves.
4. Attacker optionally submits a reverse swap to restore most of their capital, net cost limited to the pool's LP fee (e.g., 0.3%) versus the actual value of the transaction fee subsidized.
5. Repeating this lets the attacker submit heavy, weight-consuming transactions for a fraction of their intended economic cost, degrading fair fee-based spam resistance for the affected chain.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L968-1014)
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1607-1618)
```rust
sp_api::decl_runtime_apis! {
	/// This runtime api allows people to query the size of the liquidity pools
	/// and quote prices for swaps.
	pub trait AssetConversionApi<Balance, AssetId>
	where
		Balance: frame_support::traits::tokens::Balance + MaybeDisplay,
		AssetId: Codec,
	{
		/// Provides a quote for [`Pallet::swap_tokens_for_exact_tokens`].
		///
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_in_max` to control slippage.)
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
