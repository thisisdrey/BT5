### Title
Spot-price AMM quoting in `SwapAssetAdapter` (`pallet-asset-conversion-tx-payment`) lets a user self-sandwich the pool to pay near-zero transaction fees — ([File: substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs])

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` converts a user-chosen asset into the fee-native asset by calling `pallet-asset-conversion`'s `quote_price_tokens_for_exact_tokens`, which is a pure spot-price read of the pool's current reserves with no oracle, TWAP, or price-deviation check — the exact same broken invariant as the SponsorVault report (`getInGivenExpectedOut` on raw spot price). Because the reserves can be freely moved by anyone in an earlier extrinsic within the same block, an attacker can pump the pool's exchange rate immediately before their own fee-paying transaction executes, making the extension believe a tiny amount of the chosen asset is worth the full native fee, then unwind the trade afterward to recover the capital. The chain performs the full weight/length work of the dispatched call while being paid a fee determined by a manipulated, momentary price — public underpriced work, directly in the required-impact category.

### Finding Description
`SwapAssetAdapter::withdraw_fee` (called from the `ChargeAssetTxPayment` transaction extension, i.e. a pure public entry point requiring no privilege) computes how much of the user-specified asset is needed to cover the native fee purely from the AMM's live reserves: [1](#0-0) 

That amount comes from `AssetConversion::quote_price_tokens_for_exact_tokens`, which reads `get_reserves` and applies the constant-product formula directly — no TWAP, no oracle comparison, no minimum-elapsed-block/staleness check: [2](#0-1) 

Any signed account can submit, in the same block and same account (sequential nonces), two extrinsics:
1. A `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` call that pushes the pool's `asset_id`/native ratio to an extreme (e.g., dump native into the pool to make `asset_id` "expensive" relative to native).
2. Any signed extrinsic paying fees `in asset_id` (via `ChargeAssetTxPayment::from(0, Some(asset_id))`), whose `withdraw_fee` will quote almost none of `asset_id` for the full native fee because of the temporarily skewed reserves.

Optionally a third extrinsic in the same or next block reverses the swap to recover most of the capital (paying only the LP swap fee as the cost of the manipulation), the same "manipulate → exploit → unwind" pattern described in the SponsorVault report.

This is not a governance/admin/validator/relayer-privileged path: it is exploitable by an ordinary signed account solely through public extrinsics (`swap_exact_tokens_for_tokens` + any call using `ChargeAssetTxPayment`), matching the required "public underpriced work" and "unauthorized ... theft" categories while explicitly excluding malicious-validator/collator assumptions (ordering within one account's own nonces requires no block-author collusion).

### Impact Explanation
The chain executes and pays weight/length costs for the dispatched call, but the actual value extracted from the payer can be pushed arbitrarily close to zero (bounded only by the pool's LP fee and available liquidity to move the price), meaning the effective "price" charged for block space/computation is decoupled from the real economic value the fee mechanism intends to charge. This degrades the fee market's ability to price block resources correctly — the core "public underpriced work that degrades block production" impact — and, on runtimes where these fees fund a treasury/staking pot (`ResolveAssetTo<StakingPot, ...>` in the Asset Hub configs), this is a value-conservation break where the intended beneficiary receives less than the true value of consumed resources. [3](#0-2) 

### Likelihood Explanation
Likelihood is high on any runtime that enables `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` over a low-liquidity or attacker-accessible pool (e.g., custom asset pools on Asset Hub or Penpal). No special role or timing luck is required beyond controlling extrinsic order for one's own account within a block, which any user can do by paying sufficient priority/tip. The swap functions used for manipulation (`swap_exact_tokens_for_tokens`) are themselves unauthenticated public calls.

### Recommendation
`SwapAssetAdapter`/`quote_price_tokens_for_exact_tokens` should not rely purely on the instantaneous spot reserves for fee-critical pricing. Options:
- Introduce a time-weighted or previous-block-anchored price for fee quoting, and bound how far the live spot price may deviate from it before rejecting the fee payment (mirroring the SponsorVault fix of comparing spot vs. oracle/TWAP price and reverting on excessive deviation).
- Alternatively, cap per-block reserve movement impact on fee quoting or require the quote to be computed from reserves as of a prior block, not the executing block's live state.

### Proof of Concept
1. Attacker funds an asset/native pool with small liquidity (or targets an existing shallow pool used for `ChargeAssetTxPayment`).
2. In block N, nonce k: attacker calls `AssetConversion::swap_exact_tokens_for_tokens` selling a large amount of native (or asset_id) into the pool, skewing reserves so that `asset_id` is quoted as extremely valuable relative to native.
3. In block N, nonce k+1: attacker submits any call with `ChargeAssetTxPayment::from(tip, Some(asset_id))`. `SwapAssetAdapter::withdraw_fee` calls `quote_price_tokens_for_exact_tokens(asset_id, native, fee, true)` against the now-skewed reserves and withdraws a near-zero amount of `asset_id` from the attacker to cover the full native `fee`.
4. In block N (or N+1), nonce k+2: attacker calls `swap_exact_tokens_for_tokens` again to reverse the initial trade, recovering nearly all capital minus the pool's LP fee.
5. Net effect: attacker's transaction consumed full block weight/length but paid a fee far below the intended `WeightToFee`/`LengthToFee` value, verifiable by comparing `Event::AssetTxFeePaid.actual_fee` (in `asset_id` terms) against the pool price before/after step 2, per the existing test harness pattern in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs` (`transaction_payment_in_asset_possible`) which already demonstrates spot-price-driven quoting with no deviation checks. [4](#0-3)

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-157)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1571-1603)
```rust
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-rococo/src/lib.rs (L904-916)
```rust
impl pallet_asset_conversion_tx_payment::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type AssetId = xcm::v5::Location;
	type OnChargeAssetTransaction = SwapAssetAdapter<
		TokenLocation,
		NativeAndNonPoolAssets,
		AssetConversion,
		ResolveAssetTo<StakingPot, NativeAndNonPoolAssets>,
	>;
	type WeightInfo = weights::pallet_asset_conversion_tx_payment::WeightInfo<Runtime>;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = AssetConversionTxHelper;
}
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L245-256)
```rust

			let fee_in_native = base_weight + tx_weight + len as u64;
			let input_quote = AssetConversion::quote_price_tokens_for_exact_tokens(
				NativeOrWithId::WithId(asset_id),
				NativeOrWithId::Native,
				fee_in_native,
				true,
			);
			assert_eq!(input_quote, Some(201));

			let fee_in_asset = input_quote.unwrap();
			assert_eq!(Assets::balance(asset_id, caller), balance);
```
