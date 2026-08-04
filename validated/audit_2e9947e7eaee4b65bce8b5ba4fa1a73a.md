### Title
Delivery-fee pricing for XCM `PayFees`/`BuyExecution` swaps uses manipulable AMM spot price instead of a TWAP, allowing underpriced bridge/message delivery — ([File: polkadot/xcm/xcm-executor/src/lib.rs])

### Summary
The Yield report describes a price source (a Uniswap pool consulted without TWAP protection) that can return a stale/manipulable instantaneous price instead of a time-weighted average, letting a caller pay the wrong amount for a critical operation. The Polkadot SDK has a structurally identical pattern: XCM fee-in-kind computations (`PayFees`, `BuyExecution`, delivery-fee conversion for bridged messages) call `pallet_asset_conversion`'s `QuotePrice` implementation, which reads the *current* pool reserves (`get_reserves`) and computes a spot price with no TWAP or manipulation-resistance mechanism.

### Finding Description
`xcm_executor::XcmExecutor::calculate_asset_for_delivery_fees` calls `Config::AssetExchanger::quote_exchange_price` to determine how much of a held asset must be swapped to cover delivery fees for an outbound/bridged message: [1](#0-0) 

The concrete adapter used for this, `SingleAssetAdapter::quote_exchange_price`, delegates straight to `pallet_asset_conversion::QuotePrice`: [2](#0-1) 

`pallet-xcm`'s public `XcmPaymentApi` helper `query_delivery_fees` performs the same conversion for a caller-chosen `versioned_asset_id`: [3](#0-2) 

The underlying pricing functions in `pallet-asset-conversion` explicitly compute an instantaneous price from live reserves and document that the guarantee holds **only** "if no other swaps are made after the price is quoted and before the target swap ... (e.g., the swap is made immediately within the same transaction)": [4](#0-3) [5](#0-4) [6](#0-5) 

This mirrors the reported bug class exactly: a "price" is derived from a live, unprotected instantaneous state (pool reserves / mock price) rather than a manipulation-resistant time-weighted mechanism, and the consuming code (fee conversion for XCM/bridge delivery) trusts that value directly to determine how much value is charged for delivering a message.

### Impact Explanation
`calculate_asset_for_delivery_fees` and `query_delivery_fees` gate how much of an asset is actually taken from the sender to pay for message delivery — including delivery through the `XcmBridgeHubRouter` and Snowbridge exporters that route to Ethereum. If the reserves of the swap pool used for the fee asset are shallow (e.g., a newly created pool, or one an attacker seeds), a caller can, within a single transaction (e.g., via `pallet_utility::batch` or an XCM program with nested `ExchangeAsset`), push the pool's spot price away from its fair value immediately before the fee-conversion call executes, causing `quote_exchange_price`/`quote_price_tokens_for_exact_tokens` to under-quote the amount of fee asset required. The sender then pays far less than the actual cost of remote execution/delivery, while the chain/relayer/bridge infrastructure still performs the full weight of work. This is exactly the "public underpriced work that degrades block production or stalls bridge processing" impact category, because attackers can cheaply spam delivery/bridge queues once the true cost is bypassed.

### Likelihood Explanation
No privileged role is required — pool creation, `add_liquidity`/`remove_liquidity`, and `swap_exact_tokens_for_tokens` are all public extrinsics, and `pallet_xcm::execute`/`send` and the `XcmPaymentApi` are public entry points. The attack surface is highest for low-liquidity or newly-created asset-conversion pools that are nonetheless accepted by chain configuration as valid `AssetExchanger`/fee-asset pools (e.g., new foreign-asset pools set up for a bridged asset, as seen in the `bridging` config modules). The main mitigating factor is that well-established, deep-liquidity pools (e.g., DOT/USDT on Asset Hub) are expensive to move meaningfully within one transaction; likelihood is concentrated on newly-created or thin pools rather than main-network pairs.

### Recommendation
- For any fee-critical price lookup (delivery-fee conversion, `PayFees` swap sizing), do not rely purely on instantaneous `get_reserves`/spot-price quotes from `pallet_asset_conversion`. Add a manipulation-resistance mechanism: e.g., bound the allowed price deviation per block, require minimum pool liquidity/age before a pool is eligible as a fee-asset source, or maintain an on-chain moving-average price checkpointed across blocks (the on-chain analogue of the recommended Uniswap TWAP/`observe` approach).
- Alternatively, restrict `AssetExchanger` fee-asset pools to pools whitelisted by governance with liquidity floors, so an attacker cannot introduce a thin pool and immediately exploit it for underpriced delivery.
- Add a same-block/same-transaction re-entrancy or slippage guard specifically for the fee-computation path, separate from the general swap slippage parameters that only protect the swapper, not the network absorbing the delivery cost.

### Proof of Concept
1. Attacker creates (or finds) a thin `pallet_asset_conversion` pool between `AssetX` (their chosen fee asset) and the native/fee-settlement asset, satisfying minimum liquidity requirements but with small absolute reserves.
2. In a single transaction (e.g., `pallet_utility::batch_all`):
   a. Call `AssetConversion::swap_exact_tokens_for_tokens` to swap a large amount of `AssetX` into the pool, skewing reserves so that `AssetX` `->` native become artificially cheap per `get_amount_in`/`get_amount_out` math shown in `substrate/frame/asset-conversion/src/lib.rs:1499-1603`.
   b. Immediately call `pallet_xcm::execute` (or `send`) with an XCM program using `PayFees`/`BuyExecution` denominated in `AssetX`, targeting a remote destination that requires delivery-fee conversion via `calculate_asset_for_delivery_fees` (`polkadot/xcm/xcm-executor/src/lib.rs:683-700`).
   c. Because the quote is computed against the just-skewed reserves, the executor concludes only a small amount of `AssetX` is needed to cover the true native delivery fee, and the message is accepted/routed with an artificially low charge.
   d. Optionally reverse the initial swap in the same batch to restore the pool state, keeping the manipulation cost near zero (bounded only by LP fees).
3. Repeat to flood the outbound queue / bridge router with underpriced delivery requests.

Note: I was unable to fully trace whether a specific production runtime configuration (e.g., a live Asset Hub `AssetExchanger` wiring) currently exposes a thin, attacker-creatable pool as a valid fee-asset source for a bridge route in this snapshot of the repo — that would need to be checked against the live chain-spec/pool state to confirm real-world exploitability, since the vulnerability is in the shared library code (`xcm-executor`, `xcm-builder`, `pallet-asset-conversion`) rather than tied to one runtime's parameters.

### Citations

**File:** polkadot/xcm/xcm-executor/src/lib.rs (L683-700)
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

**File:** polkadot/xcm/pallet-xcm/src/lib.rs (L3303-3314)
```rust
		let assets_to_pay = if fee.id == asset_id {
			// If the fee asset is the same as the desired one, just return that.
			fees
		} else {
			// We get the fees in the desired asset.
			AssetExchanger::quote_exchange_price(
				&fees.into(),
				&(asset_id, Fungible(1)).into(),
				true, // Maximal.
			)
			.ok_or(XcmPaymentApiError::AssetNotFound)?
		};
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
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
