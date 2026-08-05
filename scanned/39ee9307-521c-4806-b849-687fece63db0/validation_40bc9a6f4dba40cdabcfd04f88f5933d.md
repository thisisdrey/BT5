## Analysis

The external report's core broken invariant: **an externally-updatable price value is consumed in fund-moving logic (mint/burn) with no staleness or outlier protection, so a single manipulated read of that price directly determines how much value a user receives.**

The closest verified local analog is in `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter`, which pays/refunds transaction fees in a non-native asset by spot-quoting an AMM pool (`pallet-asset-conversion`) — the same pool that the dispatched call itself is free to manipulate before the refund quote is taken.

### Title
Unprotected spot-price refund quote in `SwapAssetAdapter::correct_and_deposit_fee` lets a signed extrinsic drain AMM pool reserves via self-manipulated price - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`ChargeAssetTxPayment` withdraws the transaction fee in a user-chosen asset by quoting `pallet_asset_conversion`'s live pool reserves in `withdraw_fee` (`prepare` phase), then — **after the dispatched call has fully executed** — computes the unused-weight refund by taking a *second, independent* spot quote of the same pool in `correct_and_deposit_fee` (`post_dispatch_details` phase). Because the wrapped call executes between these two quotes and can itself be an `AssetConversion::swap_exact_tokens_for_tokens` call on the very same pool, the refund-time price is fully attacker-controlled within a single atomic extrinsic.<cite repo="ThankGodontt/polkadot-sdk--032" path="substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs" start="259="265" /><cite repo="ThankGodontt/polkadot-sdk--032" path="substrate/frame/transaction-payment/asset-conversion-tx-payment/src/lib.rs" start="327="343" />

### Finding Description
`SwapAssetAdapter::withdraw_fee` withdraws `asset_fee` of the user's chosen asset based on a spot quote (`S::quote_price_tokens_for_exact_tokens`) and immediately swaps it into the fee asset via `S::swap_tokens_for_exact_tokens`.<cite repo="ThankGodontt/polkadot-sdk--032" path="substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs" start="142="176" />

Later, after the wrapped `RuntimeCall` has dispatched, `correct_and_deposit_fee` computes `refund_amount` in the native fee asset from the *actual* weight used, then quotes how much of the user's chosen asset that refund is worth via `S::quote_price_exact_tokens_for_tokens(A::get(), asset_id, refund_amount, true)`, and swaps that exact amount out of the pool back to the user.<cite repo="ThankGodontt/polkadot-sdk--032" path="substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs" start="210="286" />

`pallet_asset_conversion::QuotePrice`'s own documentation states the guarantee only holds "if no other swaps are made after the price is quoted and before the target swap (e.g., the swap is made immediately within the same transaction)".<cite repo="ThankGodontt/polkadot-sdk--032" path="substrate/frame/asset-conversion/src/swap.rs" start="116="119" /> This assumption is violated by design here: between the *withdraw* quote and the *refund* quote sits the arbitrary dispatched call, which the same signed user fully controls and which uses the identical `pallet_asset_conversion::Pallet` instance/pools as the fee adapter (`AssetConversion` is shared runtime-wide, e.g. in `substrate/frame/staking-async/runtimes/parachain/src/lib.rs:967-976`). [1](#0-0) 

An unprivileged signed account can therefore submit a single extrinsic:
1. `ChargeAssetTxPayment { asset_id: X }` (paying fees in asset `X`, pooled against native asset `A`).
2. `call = AssetConversion::swap_exact_tokens_for_tokens(path=[X, A], ..., amount_out_min=1)` with a large amount, executed with the user's own funds, which sharply skews the `X`/`A` pool reserve ratio within the same block/extrinsic.

Because step 2 executes strictly between the `withdraw_fee` quote and the `correct_and_deposit_fee` quote of the *same* extrinsic, the refund computed in step (`correct_and_deposit_fee`) is derived from a reserve ratio the attacker just set. This lets the attacker extract more `X` from the pool (funded by genuine liquidity providers) than the fee logic is economically entitled to refund, or otherwise arbitrage the two quotes for profit — the same "no outlier/staleness protection on price used for fund movement" primitive as the badgerDAO report, but here it's a fully on-chain, permissionless, atomic, single-extrinsic manipulation path rather than an oracle feed.

### Impact Explanation
This breaks the "conserve value / settle exactly once to the rightful beneficiary and amount" invariant for AMM pools shared with the fee-payment mechanism: a normal user extrinsic (no admin/governance/relayer/validator involvement) can be crafted to extract value from liquidity providers of the `pallet-asset-conversion` pool via the fee-refund path, because the refund price is taken from post-manipulation reserves rather than the pre-manipulation quote used for the withdrawal. On any runtime enabling `pallet-asset-conversion-tx-payment` with `SwapAssetAdapter` against public pools (e.g., Asset Hub / staking-async parachain runtime configs shown above), this is fund-loss/theft-class impact from an unprivileged account, matching the "theft or unbacked mint" and "public underpriced work" categories in scope.

### Likelihood Explanation
Likelihood is high in principle for any runtime combination where `SwapAssetAdapter`/`ChargeAssetTxPayment` shares its pools with a generally callable swap extrinsic (the common configuration): the attack requires only a single self-authored extrinsic, no relayer, no validator collusion, and no privileged origin — exactly the unprivileged public-entrypoint path the task calls for. The actual profitability depends on pool depth/fees/slippage economics (an attacker must move the price enough to profit net of AMM fees and their own capital cost), so real-world exploitability is bounded by pool liquidity, but the structural TOCTOU (time-of-check/time-of-use) gap between the two independent spot quotes is unconditional and present in the code as written.

### Recommendation
- Do not take a fresh spot quote for the refund in `correct_and_deposit_fee`; instead, derive the refund using the exchange rate/amount recorded at `withdraw_fee` time (e.g., pro-rate the already-known `asset_fee` by `corrected_fee/fee`), so the refund never depends on a price read after the wrapped call executed.
- Alternatively, snapshot pool reserves (or an internal price) at `prepare` time and either bound the refund quote to that snapshot or reject/clamp refunds whose implied price deviates beyond a tolerance from the withdrawal-time price (an oracle-outlier-style guard, analogous to the original report's median/threshold recommendation).
- Consider disallowing/limiting swaps against the exact fee-asset pool within the same extrinsic that also pays fees via that pool, or apply slippage/TWAP protection consistent with `QuotePrice`'s stated "immediately within the same transaction" assumption, which is currently violated across the withdraw/refund boundary.

### Proof of Concept
1. Configure a runtime with `pallet_asset_conversion_tx_payment::Config::OnChargeAssetTransaction = SwapAssetAdapter<Native, Fungibles, AssetConversion, OU>` sharing pools with a publicly callable `AssetConversion::swap_exact_tokens_for_tokens`. [2](#0-1) 
2. Create/observe a pool `X/Native` with modest liquidity.
3. Submit one signed extrinsic: `ChargeAssetTxPayment{asset_id: X}` wrapping `call = AssetConversion::swap_exact_tokens_for_tokens(path=[X, Native], amount_in=large, amount_out_min=1, ...)`.
4. `withdraw_fee` withdraws `asset_fee` of `X` using the pre-manipulation reserve quote and swaps it for the native fee asset. [3](#0-2) 
5. The wrapped call executes, swapping a large amount of `X` for `Native` and materially shifting the pool's `X/Native` ratio.
6. `correct_and_deposit_fee` computes `refund_amount` in `Native` from actual weight used, then calls `S::quote_price_exact_tokens_for_tokens(Native, X, refund_amount, true)` against the now-skewed pool, producing an `refund_asset_amount` of `X` that is inflated relative to the fair pre-manipulation price, and swaps it out of the pool to the attacker. [4](#0-3) 
7. Net effect: attacker's single extrinsic nets more `X` back than the fee logic intends, funded by the pool's liquidity providers, with no governance, validator, or relayer involvement required.

### Citations

**File:** substrate/frame/staking-async/runtimes/parachain/src/lib.rs (L967-979)
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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L259-289)
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
```
