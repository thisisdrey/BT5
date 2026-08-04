### Title
Unauthorized, unrestricted initial pool-price setting in `pallet-asset-conversion` enables underpriced fee payment via `pallet-asset-conversion-tx-payment` - (File: `substrate/frame/asset-conversion/src/lib.rs`, `substrate/frame/transaction-payment/asset-conversion-tx-payment/src/payment.rs`)

### Summary
The seed report describes an unprotected `initialize` function that lets an unprivileged actor set the initial price of an AMM pool with no sanity checks. The local analog is `pallet-asset-conversion`'s permissionless `create_pool`/`add_liquidity` flow: any signed account can create a pool and, as first liquidity provider, set an arbitrary reserve ratio with zero price validation. This "initial price" is then consumed directly and trustingly by `pallet-asset-conversion-tx-payment`'s `SwapAssetAdapter::withdraw_fee` to compute how much of a non-native asset is charged for transaction fees, with no floor/oracle cross-check.

### Finding Description
`Pallet::create_pool` (`substrate/frame/asset-conversion/src/lib.rs:442-450`) is callable by any signed origin (`ensure_signed(origin)?`), and `do_add_liquidity` (`substrate/frame/asset-conversion/src/lib.rs:790-892`) explicitly special-cases the first deposit into an empty pool:

```rust
if reserve1.is_zero() || reserve2.is_zero() {
    amount1 = amount1_desired;
    amount2 = amount2_desired;
} else { ... }
``` [1](#0-0) 

This means the very first liquidity provider fully controls the reserve ratio (i.e., the "spot price") of the pool, with no minimum liquidity depth requirement beyond `T::MintMinLiquidity` and no external price reference check — structurally identical to AlgebraPool's `initialize(initialPrice)` which just accepts whatever price the caller supplies once (`globalState.price == 0` check only, no oracle bound). [2](#0-1) 

Unlike the original report (which required front-running someone else's initialize transaction), here the attacker needs **no front-running at all** — they simply create the pool and add the first liquidity themselves, unilaterally and atomically, fully controlling the resulting spot price.

This attacker-controlled price then feeds directly into `pallet-asset-conversion-tx-payment`, which is designed to let users pay transaction fees in non-native assets by swapping through `pallet-asset-conversion` pools:

```rust
let asset_fee =
    S::quote_price_tokens_for_exact_tokens(asset_id.clone(), A::get(), fee, true)
        .filter(|asset_fee| !asset_fee.is_zero())
        .ok_or(InvalidTransaction::Payment)?;
``` [3](#0-2) 

`quote_price_tokens_for_exact_tokens` reads live pool reserves (`get_reserves`) with no floor, TWAP, or external reference price — it is a naive constant-product spot quote. Since the attacker set the reserves at pool creation, they can make one unit of `asset_id` "worth" an arbitrarily large amount of the native fee asset, meaning the amount of `asset_id` withdrawn from the payer (`asset_fee`) for a given native `fee` can be made near-zero.

### Impact Explanation
An attacker who wants to spam blocks cheaply can:
1. Mint/acquire a nearly worthless custom asset (or any asset they control).
2. Call `create_pool` + `add_liquidity` themselves, setting the pool ratio so that a huge amount of native fee corresponds to a negligible amount of `asset_id`.
3. Submit transactions using `ChargeAssetTxPayment` with `asset_id` set to their manipulated asset, paying fees at a fraction of the true native-fee cost.

This is "public underpriced work that degrades block production": the runtime believes fees were paid at fair value (converted at pool spot price) but the payer effectively obtained block space almost for free, since the "market price" was self-set with no minimum liquidity/depth or oracle safeguard. This directly undermines the fee mechanism's DoS-resistance economic assumption for any runtime that enables `pallet-asset-conversion-tx-payment` with permissionless pool creation (e.g., Asset Hub runtimes), without requiring any privileged actor, front-running, or malicious relayer/validator — purely an unprivileged, self-contained sequence of two ordinary calls.

### Likelihood Explanation
High for any deployment that (a) allows permissionless `create_pool`/`add_liquidity` (default, no `AdminOrigin` gate on the plain `create_pool` call) and (b) wires `SwapAssetAdapter`/`ChargeAssetTxPayment` to accept arbitrary `asset_id`s for fee payment against those pools. No governance, key compromise, or peer/validator collusion is needed — a single unprivileged account executing two extrinsics is sufficient.

### Recommendation
- Require a minimum reserve/liquidity depth or a bounded initial price deviation check before a pool can be used by `SwapAssetAdapter` for fee conversion (e.g., require the pool to have been active for N blocks and/or reserves above a configurable threshold).
- Consider using a manipulation-resistant price source (e.g., TWAP over multiple blocks) rather than instantaneous spot price in `quote_price_tokens_for_exact_tokens` when used for fee payment.
- Alternatively, restrict which `asset_id`s are eligible for `ChargeAssetTxPayment` to an allow-list maintained by governance (`AdminOrigin`-gated), rather than any asset with an existing permissionless pool.

### Proof of Concept
1. Attacker (unprivileged, signed) calls `AssetConversion::create_pool(origin, native, custom_asset)`. [2](#0-1) 
2. Attacker calls `AssetConversion::add_liquidity(origin, native, custom_asset, amount1_desired=1, amount2_desired=1_000_000_000_000, ...)` — since reserves are zero, both amounts are accepted verbatim, setting an extreme price ratio. [4](#0-3) 
3. Attacker submits ordinary transactions using `ChargeAssetTxPayment { asset_id: Some(custom_asset), .. }`. `withdraw_fee` quotes `asset_fee` via `quote_price_tokens_for_exact_tokens` against the manipulated pool, charging a negligible amount of `custom_asset` for a large native-equivalent fee. [5](#0-4) 
4. Repeating this lets the attacker flood the chain with transactions paying near-zero effective fees, degrading block production economics without needing to front-run any other party.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L442-450)
```rust
		pub fn create_pool(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_create_pool(&sender, *asset1, *asset2, None)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L816-856)
```rust
			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;

				if amount2_optimal <= amount2_desired {
					ensure!(
						amount2_optimal >= amount2_min,
						Error::<T>::AssetTwoDepositDidNotMeetMinimum
					);
					amount1 = amount1_desired;
					amount2 = amount2_optimal;
				} else {
					let amount1_optimal = Self::quote(&amount2_desired, &reserve2, &reserve1)?;
					ensure!(
						amount1_optimal <= amount1_desired,
						Error::<T>::OptimalAmountLessThanDesired
					);
					ensure!(
						amount1_optimal >= amount1_min,
						Error::<T>::AssetOneDepositDidNotMeetMinimum
					);
					amount1 = amount1_optimal;
					amount2 = amount2_desired;
				}
			}

			ensure!(
				amount1.saturating_add(reserve1) >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::AmountOneLessThanMinimal
			);
			ensure!(
				amount2.saturating_add(reserve2) >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::AmountTwoLessThanMinimal
			);

			T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
			T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
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
