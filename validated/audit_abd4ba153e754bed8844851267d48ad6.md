## Analysis

**Core broken invariant from the C4 report:** a value calculation combines a manipulation-resistant reference (Chainlink price) with an amount that is derived from the live, atomically-manipulable AMM spot state (`slot0()` reserves), and the guard (`_checkPoolPrice`) only validates the price component, not the amount component — so an attacker can distort the spot reserves in the same transaction/block and profit even though the "price" check passes.

**Local analog found:** `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee`/`can_withdraw_fee` and the refund path in `payment.rs`, which price a transaction fee by calling directly into `pallet-asset-conversion`'s live spot reserves via `quote_price_tokens_for_exact_tokens` / `quote_price_exact_tokens_for_tokens`. [1](#0-0) [2](#0-1) 

The quote is computed directly from `AssetConversion::get_reserves`, i.e. the current pool account token balances — no TWAP, no external reference price, no cross-check against manipulation: [3](#0-2) [4](#0-3) 

The `QuotePrice` trait doc itself acknowledges the price is only trustworthy if untouched "within the same transaction," which is exactly the property the C4 finding shows is insufficient once an attacker controls prior extrinsics in the same block: [5](#0-4) 

### Title
Transaction-fee-in-asset pricing relies on unprotected AMM spot reserves, enabling same-block fee-price manipulation - (File: `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
`SwapAssetAdapter::withdraw_fee` (used by `ChargeAssetTxPayment` to let users pay fees in a non-native asset) determines how much of a user's chosen asset to withdraw by quoting the price against `pallet-asset-conversion`'s live pool reserves. Those reserves are the same mutable state anyone can shift with an ordinary `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsic. There is no TWAP, oracle cross-check, or manipulation-resistance mechanism analogous to `V3Oracle`'s Chainlink comparison — the only thing checked is that the pool can still deliver the quoted amount (`reducible_balance`), not whether the *price* itself has been distorted.

### Finding Description
`withdraw_fee` calls `S::quote_price_tokens_for_exact_tokens(asset_id, A::get(), fee, true)`, which internally calls `AssetConversion::get_reserves`, reading the pool account's current token balances and computing an AMM-formula quote from them (`get_amount_in`/`get_amount_out`/`quote`). This is analogous to `V3Oracle::_getAmounts` reading `pool.slot0()` spot price to size `amount0`/`amount1`. In both cases the amount used to settle value (fee amount here, position amount there) is derived from momentarily-controllable AMM state rather than a robust reference price, while the surrounding checks (reserve-availability check here, `_checkPoolPrice` there) validate a different thing than what actually gets manipulated.

An attacker who is also a liquidity holder/trader can, within the same block, submit an ordinary swap extrinsic that shifts the pool reserve ratio for `asset_id`/native, then immediately submit their real transaction paying fees in `asset_id`. The fee-asset amount charged (`asset_fee`) is computed from the *post-manipulation* reserves. Because reserves are shared global chain state and any account can trade against them with a plain, permissionless `swap_tokens_for_exact_tokens` call, no privileged actor, relayer, or validator collusion is required — this is directly analogous to the flash-loan-style, single-block price distortion described in the C4 report, just achieved via sequential self-owned extrinsics instead of a flash loan.

### Impact Explanation
By transiently distorting the `asset_id`/native reserve ratio, an attacker can make the AMM "believe" `asset_id` is more valuable in native terms than it should be, causing `quote_price_tokens_for_exact_tokens` to under-quote the amount of `asset_id` needed to cover a fixed native fee. The subsequent `S::swap_tokens_for_exact_tokens` executes at that same distorted rate, extracting real native-equivalent liquidity from the pool relative to what the attacker contributes. This is a form of "public underpriced work" (transaction inclusion) that also drains value from other liquidity providers in the pool, since the swap executed to settle the fee is not economically neutral at the distorted price.

### Likelihood Explanation
The AMM pool and the tx-payment adapter are both permissionless, live-scope components (`pallet-asset-conversion`, `pallet-asset-conversion-tx-payment`) used on Asset Hub runtimes. Any account can trigger the distortion with an ordinary, unprivileged swap extrinsic combined with a fee-paying extrinsic in the same block — no admin, governance, relayer, or validator role is needed, matching the "unprivileged attacker" requirement.

### Recommendation
Do not size the fee-asset amount purely off the pool's instantaneous spot reserves. Options: require the fee-conversion quote to be bounded by a max/min slippage parameter supplied by the user (analogous to `amount_in_max`/`amount_out_min` used elsewhere in the pallet), or use a longer-window/reference price (e.g., a moving average or oracle) for tx-fee conversion, similar to the mitigation Revert Finance applied (basing amounts on the oracle price rather than spot price).

### Proof of Concept
1. Attacker holds asset `X` and native tokens; a `NativeOrWithId::WithId(X)`/native pool exists via `pallet-asset-conversion`.
2. In extrinsic 1 (same block), attacker calls `AssetConversion::swap_tokens_for_exact_tokens` (or `swap_exact_tokens_for_tokens`) to shift the pool's `X`/native reserve ratio so that `X` is temporarily overvalued in native terms.
3. In extrinsic 2 (same block, immediately after), attacker submits a transaction with `ChargeAssetTxPayment::from(tip, Some(X))`; `SwapAssetAdapter::withdraw_fee` calls `AssetConversion::quote_price_tokens_for_exact_tokens(X, Native, fee, true)` — see [1](#0-0)  — which reads the just-manipulated reserves via `get_reserves` — see [3](#0-2) .
4. The quoted (and then swapped) amount of `X` withdrawn from the attacker is computed from the distorted reserves, letting the attacker settle the fixed native-denominated fee while paying disproportionately little real value, at the pool's expense.
5. No component in this path (reserve-availability check, `min_balance` check) validates that the reserve ratio itself is undistorted, mirroring the gap the C4 report identified in `V3Oracle::getValue()`.

### Citations

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L142-146)
```rust
		// Quote the amount of the `asset_id` needed to pay the fee in the asset `A`.
		let asset_fee =
			S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
				.filter(|asset_fee| !asset_fee.is_zero())
				.ok_or(InvalidTransaction::Payment)?;
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs (L159-176)
```rust
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

**File:** substrate/frame/asset-conversion/src/swap.rs (L116-120)
```rust
/// Trait providing methods to quote swap prices between asset classes.
///
/// The quoted price is only guaranteed if no other swaps are made after the price is quoted and
/// before the target swap (e.g., the swap is made immediately within the same transaction).
pub trait QuotePrice {
```
