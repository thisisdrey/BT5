### Title
XCM delivery-fee computation trusts a manipulable single-block AMM spot price with no bound/staleness check, enabling underpriced bridge delivery fees - ([File: polkadot/xcm/xcm-executor/src/lib.rs])

### Summary
The Chainlink report's core broken invariant is: *a price source that can silently diverge from the true market value is trusted directly by downstream logic with no sanity bound, causing mispriced execution.* The local analog is `pallet_asset_conversion`'s `QuotePrice::quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens`, which return a **live AMM spot price** derived from the pool's current on-chain reserves, with no TWAP, no min/max sanity bound, and no staleness protection. This price is consumed directly by `xcm_executor::XcmExecutor::calculate_asset_for_delivery_fees` / `take_fee` (via `Config::AssetExchanger::quote_exchange_price`, implemented by `SingleAssetExchangeAdapter`) to determine how much of a user-supplied asset must be swapped to cover Snowbridge/XCM delivery fees, and by `SwapAssetAdapter::withdraw_fee` for transaction fee payment.

### Finding Description
`pallet_asset_conversion::Pallet::quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` compute the swap price purely from the pool's current reserve balances (`Self::get_reserves`) at call time. [1](#0-0) 
The trait doc explicitly warns the quote is only meaningful "if no other swaps are made after the price is quoted", i.e. it is a raw spot price with no manipulation resistance. [2](#0-1) 

This price feeds directly into `SingleAssetExchangeAdapter::quote_exchange_price`, which is the `AssetExchange` implementation configured as `Config::AssetExchanger` in the XCM executor. [3](#0-2) 

The XCM executor's `calculate_asset_for_delivery_fees` calls `Config::AssetExchanger::quote_exchange_price` to determine how much of the asset actually held (e.g. from `PayFees`/`BuyExecution`) must be converted to the asset required for delivery fees, and `take_fee` then performs the corresponding `exchange_asset` swap using that same manipulable pool. [4](#0-3) [5](#0-4) 

Because XCM instructions inside a single message execute atomically and sequentially against live pallet storage, an unprivileged attacker can craft a message that first executes an `ExchangeAsset`/large swap instruction that skews the pool reserves (e.g., dumping a large amount of the fee-asset into the pool), then immediately issues `PayFees`/`InitiateAssetsTransfer` that triggers `calculate_asset_for_delivery_fees`/`take_fee` while the pool is still in the attacker-distorted state. The quoted/settled delivery fee is then computed from this transiently corrupted reserve ratio rather than the market-consistent price, letting the attacker pay far less DOT-equivalent value than the true cost of remote (Ethereum-side) message execution while the code accepts it as valid because there is no bound check comparing the quoted rate against a reference/oracle price — the exact “continues to use the corrupted extremum value as truth” pattern from the Chainlink `minAnswer` bug, just realized via AMM spot-price manipulation instead of a stale oracle floor/ceiling.

### Impact Explanation
Snowbridge's `Params.Ratio("ETH/DOT")`-based fee formula assumes the exchange rate used for fee computation reflects a fair market rate; the outbound-queue's own `PricingParameters::validate` only checks non-zero, not boundedness, and governance-set rates are a separate, deliberately out-of-scope control. [6](#0-5) 
When the *live-quoted, swap-based* AH-side delivery-fee conversion (rather than the governance rate) is used, an attacker who can manipulate pool reserves within the same message can systematically underpay delivery/relayer-reward fees for cross-chain messages. This degrades relayer incentives and can stall Snowbridge message processing (bridge queue backlog), matching the accepted "public underpriced work that degrades block production or stalls bridge processing" impact category.

### Likelihood Explanation
Any unprivileged user who can afford to temporarily move a pool's reserves (even via a flash-style same-message sequence of instructions) can trigger this; no validator, relayer, governance, or leaked-key assumption is required. The likelihood is bounded mainly by available capital to shift the specific asset pool used for delivery-fee payment, which is realistic for shallow/thin pools such as Asset Hub's DOT/WETH pairs.

### Recommendation
- Do not use raw, single-block AMM spot quotes (`quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens`) as the sole price source for delivery-fee or cross-chain settlement accounting.
- Require `calculate_asset_for_delivery_fees`/`take_fee` (and `SwapAssetAdapter`) to validate the quoted rate against a bounded reference (e.g., a TWAP, or the governance-set `PricingParameters.exchange_rate` used by `snowbridge-pallet-outbound-queue`), rejecting or reverting the message if the AMM-quoted price deviates beyond an acceptable band — the direct analog of "revert unless `minAnswer < answer < maxAnswer`".
- Alternatively, disallow using the same pool for both price discovery and immediate swap execution within a single atomic XCM program, or require multi-block/TWAP settlement before delivery-fee amounts are finalized.

### Proof of Concept
1. Attacker funds an XCM program (via `pallet_xcm::execute` or a remote `Transact`) that is processed atomically by the XCM executor configured with `SingleAssetExchangeAdapter` over a shallow AssetX/DOT pool on Asset Hub.
2. Step 1 instruction: `ExchangeAsset` a large amount of AssetX into the pool, sharply distorting the AssetX/DOT reserve ratio (temporarily making DOT "cheap" in AssetX terms).
3. Step 2 instruction: `PayFees(AssetX, small_amount)` followed by `InitiateAssetsTransfer`/`ExportMessage` destined for Ethereum via Snowbridge.
4. The executor's `take_fee` invokes `calculate_asset_for_delivery_fees`, which calls `SingleAssetExchangeAdapter::quote_exchange_price` against the now-distorted pool; `quote_price_tokens_for_exact_tokens` returns a `small_amount` of AssetX as "necessary" to cover the DOT-denominated delivery fee.
5. `exchange_asset` executes the swap at this distorted rate, and the message is enqueued to `snowbridge-pallet-outbound-queue`/`process_message` with a real relayer reward far below the message's true remote-execution cost, since `pricing_params.rewards.remote`/`fee_per_gas` on the Ethereum side is fixed but the DOT paid locally was computed off a manipulated spot price rather than `PricingParameters::exchange_rate`.
6. Repeated at scale, this either drains value from liquidity providers/the fee-conversion mechanism relative to true cost, or causes systemic underpayment of Snowbridge delivery/relayer fees, degrading relayer participation and stalling bridge message processing.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1546)
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
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```

**File:** polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs (L176-229)
```rust
	fn quote_exchange_price(give: &Assets, want: &Assets, maximal: bool) -> Option<Assets> {
		if give.len() != 1 || want.len() != 1 {
			return None;
		} // We only support 1 asset in `give` or `want`.
		let give_asset = give.get(0)?;
		let want_asset = want.get(0)?;
		// We first match both XCM assets to the asset ID types `AssetConversion` can handle.
		let (give_asset_id, give_amount) = Matcher::matches_fungibles(give_asset)
			.map_err(|error| {
				tracing::trace!(
					target: "xcm::SingleAssetExchangeAdapter::quote_exchange_price",
					?give_asset,
					?error,
					"Could not map XCM asset to FRAME asset."
				);
				()
			})
			.ok()?;
		let (want_asset_id, want_amount) = Matcher::matches_fungibles(want_asset)
			.map_err(|error| {
				tracing::trace!(
					target: "xcm::SingleAssetExchangeAdapter::quote_exchange_price",
					?want_asset,
					?error,
					"Could not map XCM asset to FRAME asset"
				);
				()
			})
			.ok()?;
		// We quote the price.
		if maximal {
			// The amount of `want` resulting from swapping `give`.
			let resulting_want =
				<AssetConversion as QuotePrice>::quote_price_exact_tokens_for_tokens(
					give_asset_id,
					want_asset_id,
					give_amount,
					true, // Include fee.
				)?;

			Some((want_asset.id.clone(), resulting_want).into())
		} else {
			// The `give` amount required to obtain `want`.
			let necessary_give =
				<AssetConversion as QuotePrice>::quote_price_tokens_for_exact_tokens(
					give_asset_id,
					want_asset_id,
					want_amount,
					true, // Include fee.
				)?;

			Some((give_asset.id.clone(), necessary_give).into())
		}
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L651-675)
```rust
		// We perform the swap, if needed, to pay fees.
		let paid = if asset_to_pay_for_fees.id != asset_needed_for_fees.id {
			Config::AssetExchanger::exchange_asset(
				self.origin_ref(),
				withdrawn_fee_asset,
				&asset_needed_for_fees.clone().into(),
				false,
			)
			.map_err(|given_assets| {
				tracing::error!(
					target: "xcm::fees",
					?given_assets, ?asset_needed_for_fees, "Swap was deemed necessary but couldn't be done:",
				);
				self.fees.subsume_assets(given_assets);
				XcmError::FeesNotMet
			})?
		} else {
			// If the asset wanted to pay for fees is the one that was needed,
			// we don't need to do any swap.
			// We just use the assets withdrawn or taken from holding.
			withdrawn_fee_asset
		};
		Config::FeeManager::handle_fee(paid, Some(&self.context), reason);
		Ok(())
	}
```

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L683-716)
```rust
	fn calculate_asset_for_delivery_fees(&self, asset_needed_for_fees: Asset) -> Asset {
		let Some(asset_wanted_for_fees) =
			// we try to swap first asset in the fees register (should only ever be one),
			self.fees.fungible.first_key_value().map(|(id, _)| id).or_else(|| {
				// or the one used in BuyExecution
				self.asset_used_in_buy_execution.as_ref()
			})
			// if it is different than what we need
			.filter(|&id| asset_needed_for_fees.id.ne(id))
		else {
			// either nothing to swap or we're already holding the right asset
			return asset_needed_for_fees
		};
		Config::AssetExchanger::quote_exchange_price(
			&(asset_wanted_for_fees.clone(), Fungible(0)).into(),
			&asset_needed_for_fees.clone().into(),
			false, // Minimal.
		)
		.and_then(|necessary_assets| {
			// We only use the first asset for fees.
			// If this is not enough to swap for the fee asset then it will error later down
			// the line.
			necessary_assets.into_inner().into_iter().next()
		})
		.unwrap_or_else(|| {
			// If we can't convert, then we return the original asset.
			// It will error later in any case.
			tracing::trace!(
				target: "xcm::calculate_asset_for_delivery_fees",
				?asset_wanted_for_fees, "Could not convert fees",
			);
			asset_needed_for_fees
		})
	}
```

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
	}
```
