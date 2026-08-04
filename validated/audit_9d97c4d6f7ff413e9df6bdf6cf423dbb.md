### Title
Transaction fees paid in non-native assets are priced from manipulable spot AMM reserves with no TWAP protection, letting a payer self-sandwich the pool to underpay fees and drain liquidity providers - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` prices non-native fee payments via `QuotePrice::quote_price_tokens_for_exact_tokens`, which is implemented by `pallet-asset-conversion` directly against the pool's *current* spot reserves (`get_reserves` / `get_amount_in`). There is no time-weighted averaging or any per-block manipulation resistance — the same broken invariant flagged in the external Stableswap report (spot/near-spot price used where an averaged price is needed). The trait itself documents this as an accepted risk only for the *single-transaction* case ("guaranteed if... the swap is made immediately within the same transaction"), but nothing prevents an attacker from manipulating the reserves with a preceding extrinsic in the same block and reaping the mispriced quote in a subsequent extrinsic, then reversing the manipulation to recover capital — a classic same-block sandwich, fully achievable by an unprivileged signed account.

### Finding Description
`quote_price_tokens_for_exact_tokens` reads `T::PoolLocator::pool_address` balances at call time and computes the amount via `get_amount_in`, using the AMM constant-product formula against whatever the reserves happen to be in that block: [1](#0-0) 

The `QuotePrice` trait explicitly documents this limitation without providing any mitigation such as a TWAP: [2](#0-1) 

`SwapAssetAdapter::withdraw_fee` uses this quote to determine how much of the payer's asset to withdraw, then immediately performs the AMM swap for the *exact* quoted amount: [3](#0-2) 

`withdraw_fee` runs as part of the `ChargeAssetTxPayment` transaction extension, i.e., on every signed extrinsic that opts to pay fees in a non-native asset — this is a fully public, unprivileged path invoked automatically for ordinary transactions, not an admin or governance action.

Because the quote and the resulting fee-swap both read the pool's live reserves at execution time, and a single block can contain multiple extrinsics from the same signer/account executed in nonce order, an attacker can:
1. Submit extrinsic #1: a large `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` call that pushes the `asset_id`↔native reserve ratio to make `asset_id` artificially cheap relative to native.
2. Submit extrinsic #2 in the same block: any call using `ChargeAssetTxPayment` with `asset_id` as the fee asset — `withdraw_fee` will quote and withdraw a drastically reduced amount of `asset_id` for the (fixed) native-denominated fee, because it reads the reserves manipulated in step 1.
3. Submit extrinsic #3 in the same block: reverse the swap from step 1 to recover most of the capital used to manipulate the pool.

The net effect: the attacker pays far less real value for their transaction fee than intended, and the value gap is extracted from the AMM pool (i.e., from liquidity providers), because `withdraw_fee`'s swap executes against the pool at the manipulated ratio and the pool absorbs the imbalance. Existing guards do not stop this: there is no minimum observation window, no TWAP, no fee price staleness/deviation check, and `can_withdraw_fee`/`withdraw_fee` only check that the quote is non-zero and withdrawable — they do not validate that the quoted price is consistent with any longer-term reference price.

### Impact Explanation
This breaks the "Balances, assets, ... treasury spends, ... contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount" pivot: the AMM pool (and its liquidity providers) settle less value than they should for the fee-asset swap, and network transaction fees can be systematically underpaid using entirely public extrinsics available to any signed account holding pool-tradeable assets. This is a chain-level economic integrity issue (public underpriced work degrading intended fee-collection guarantees), not merely a griefing/self-harm scenario, since it results in real fund loss to third-party liquidity providers in the pool.

### Likelihood Explanation
Requires only a signed account with sufficient capital to move the pool for a single block and ordinary transaction submission — no malicious validator, collator, relayer, or governance action. Any pool with modest liquidity relative to the attacker's capital is susceptible; block authors do not need to be complicit since the attacker's own transactions, submitted with appropriate nonces/priority, can be included in the same block in the required order.

### Recommendation
Do not use the raw AMM spot quote from `pallet-asset-conversion` for pricing mandatory fee payments. Either:
- Introduce a manipulation-resistant reference price (TWAP over multiple blocks, similar to the Uniswap V2 oracle pattern referenced in the source report) for `QuotePrice` implementations used by fee-payment adapters, or
- Bound the acceptable deviation between the spot quote and a longer-window reference price in `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee`, rejecting the fee payment if the deviation exceeds a safe threshold, or
- Require a minimum swap slippage/price-impact cap so a single-block manipulation cannot produce an economically exploitable quote.

### Proof of Concept
1. Set up a native/`asset_id` pool with liquidity `L_native`/`L_asset`.
2. Attacker extrinsic A (`swap_tokens_for_exact_tokens` or `swap_exact_tokens_for_tokens`): swap a large amount of native into the pool for `asset_id`, sharply reducing `asset_id`'s reserve-implied price relative to native.
3. Attacker extrinsic B in the same block: submit any call using `ChargeAssetTxPayment::from(tip, Some(asset_id))`; `withdraw_fee` calls `quote_price_tokens_for_exact_tokens(asset_id, native, fee, true)` against the now-skewed reserves (as exercised functionally in `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs`, e.g. `transaction_payment_in_asset_possible`, lines 211-300) — asset_fee is quoted far below its fair value.
4. Attacker extrinsic C in the same block: swap back `asset_id` for native to restore the pool ratio and reclaim most of the native spent in step 2.
5. Net result: attacker pays a fraction of the intended fee value in `asset_id`; the pool (LPs) absorbs the shortfall, since the fee-swap executed against the manipulated reserves in step 3. [1](#0-0) [3](#0-2) [2](#0-1)

### Citations

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
