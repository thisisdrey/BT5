## Title
`SingleAssetExchangeAdapter::quote_exchange_price` prices XCM delivery/execution fees off a manipulable spot-price AMM pool with no TWAP or deviation guard, letting an attacker self-manipulate the pool to underpay fees - ([File: polkadot/xcm/xcm-builder/src/asset_exchange/single_asset_adapter/adapter.rs])

### Summary
The external report's core broken invariant is: a single, unguarded price source (BTC/USD feed) is used as ground truth for a *different* asset (WBTC) whose real value can diverge from that source, and nothing detects the divergence before it is used to authorize value-bearing actions (minting debt). The direct Polkadot SDK analog is `SingleAssetExchangeAdapter::quote_exchange_price`, used by the XCM executor's `calculate_asset_for_delivery_fees` to price how much of a user-held asset must be swapped to pay delivery/execution fees. It reads the instantaneous reserve ratio of a `pallet-asset-conversion` pool with no staleness check, no TWAP, and no deviation guard against any secondary reference price - exactly the single-oracle pattern the report calls out.

### Finding Description
`calculate_asset_for_delivery_fees` in [1](#0-0)  calls `Config::AssetExchanger::quote_exchange_price` to determine how much of the asset a user is holding (`asset_wanted_for_fees`) must be swapped for the asset actually needed to pay delivery fees (`asset_needed_for_fees`). This quoted amount is then withdrawn from the user/holding and swapped via `exchange_asset` in `take_fee` at [2](#0-1) .

The concrete `AssetExchanger` implementation used across production runtimes (asset-hub, bridge-hub, collectives, coretime, people, penpal, relay chains) is `SingleAssetExchangeAdapter`, whose `quote_exchange_price` at [3](#0-2)  directly forwards to `pallet_asset_conversion::QuotePrice::quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens`. Those functions compute the price purely from the pool's *current* reserves at [4](#0-3) , and the trait doc itself states the guarantee only holds "if no other swaps are made after the price is quoted and before the target swap" ( [5](#0-4) ).

Because a single unprivileged user fully controls the ordering of calls inside their own transaction/batch (e.g., via `pallet_utility::batch` or `pallet_xcm::execute` with an embedded `ExchangeAsset`), they can atomically: (1) swap a large amount into/out of the fee-relevant pool to skew its reserve ratio, (2) then issue the XCM/dispatch whose `PayFees`/`BuyExecution` fee is computed from that now-skewed spot price via `calculate_asset_for_delivery_fees`, and (3) reverse the initial swap after. There is no secondary price source, TWAP, or bounded-deviation check comparable to the "double oracle" the report recommends - unlike `PriceFeed.sol`'s (still-insufficient) staleness/deviation checks, this path has none at all.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing": delivery/execution fees exist specifically to make relayers/collators/the message queue whole for processing and forwarding messages (including cross-consensus/Snowbridge-bound XCMs that use `ExportMessage` and pay delivery fees priced through this same mechanism). If the fee-in-asset conversion can be manipulated to systematically undervalue the swapped-in asset relative to the asset actually needed for fees, users can pay artificially cheap fees for message forwarding/execution, degrading the economic assumptions that back relayer compensation and message-queue throughput, and allowing execution of otherwise fee-gated operations for less than intended value - a value-conservation violation on the "fees" side of the invariant class.

### Likelihood Explanation
The attack requires only an unprivileged account holding funds in the target pool's two assets and the ability to batch calls or construct one composite XCM program that both perturbs the pool and consumes the mispriced quote - no validator, collator, relayer, prover, or governance/admin action is needed. The main constraints are available capital and pool liquidity depth (thinner pools are cheaper to move), which is a normal, attacker-controlled precondition rather than a privileged one.

### Recommendation
- Do not price delivery/execution fees from an instantaneous AMM spot price computed inside the same execution as the fee-consuming action; require a time-weighted or previous-block-anchored price, or bound the deviation between the quoted price and a recent reference price before accepting it.
- Alternatively, require `quote_exchange_price` consumers to re-validate the quote against pool state from a prior block, or impose a maximum allowed swap-amount to pool-reserve ratio so a single caller cannot materially move the price used for their own fee calculation within one execution context.

### Proof of Concept
Conceptually (cannot be executed without live pool state/tooling access here):
1. Attacker holds assets A and B, with an `AssetConversion` pool for A/B that also backs `SingleAssetExchangeAdapter`.
2. Attacker submits `pallet_utility::batch_all` (or a single XCM program) containing:
   a. `pallet_asset_conversion::swap_exact_tokens_for_tokens` (or an `ExchangeAsset` instruction) that pushes a large amount of A into the pool, skewing the B/A reserve ratio in the attacker's favor.
   b. A `PayFees`/`BuyExecution` XCM instruction denominated in B that triggers `take_fee` → `calculate_asset_for_delivery_fees` → `quote_exchange_price`, which now returns an artificially low "necessary_give" amount of B for the required fee asset (per [6](#0-5) ).
   c. A reverse swap restoring the pool close to its original ratio.
3. All three steps execute atomically for the same signed origin, so the attacker pays a fee computed off a price they themselves set moments earlier, with no oracle/TWAP cross-check to reject the divergence.

Because I could not execute this against a live runtime/test harness in this session, I was unable to directly measure the magnitude of fee underpayment for a specific pool configuration; a Devin session with test-runtime access should build a concrete `asset-conversion` + `xcm-executor` integration test reproducing the batched swap-then-pay-fees sequence to quantify the discount achievable relative to pool depth.

### Citations

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1583-1602)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
