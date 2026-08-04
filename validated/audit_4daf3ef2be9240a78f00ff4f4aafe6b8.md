### Title
`pallet-asset-conversion` treats the pool sovereign account's *raw total balance* as the AMM reserve, allowing anyone to corrupt reserve-based pricing and LP-mint math via direct token donations - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion`'s `get_balance`/`get_reserves` read the full on-chain balance of the pool's sovereign account and use it directly as the AMM reserve for both liquidity-provision math (`do_add_liquidity`, `do_remove_liquidity`) and swap pricing (`quote_price_exact_tokens_for_tokens`, `get_amount_in`/`get_amount_out`). Because the pool account is a normal, publicly transferable `AccountId`, anyone can inflate the "reserve" used in these formulas simply by sending tokens to it outside of `add_liquidity`, without minting any corresponding LP-token accounting. This is the same root-cause pattern as H-35: a contract/pallet uses the *actual token balance of an account* (which mixes legitimately-tracked funds with externally-injected value) as the authoritative amount for share-calculation and payout logic, instead of maintaining an independently tracked reserve/accounting variable.

### Finding Description
`get_balance` simply forwards to the underlying asset pallet's raw balance query: [1](#0-0) 

This is used to compute `reserve1`/`reserve2` inside `do_add_liquidity`: [2](#0-1) 

and to compute LP-token issuance proportionally against `total_supply`: [3](#0-2) 

Critically, the asset transfers into the pool account happen *before* the sanity check that the newly minted LP-token amount is non-trivial: [4](#0-3) 

The same raw-balance reserve is reused for pricing quotes consumed elsewhere in the runtime (e.g. by `pallet-asset-conversion-tx-payment` to price non-native transaction fees): [5](#0-4) [6](#0-5) 

A repository prdoc explicitly documents that this pallet *intentionally* reads the pool account's full raw balance (rather than a tracked/reducible amount) for exactly this class of calculation, confirming the design surface: [7](#0-6) 

Because there is no independent "reserve" storage item separate from the pool account's live balance, an unprivileged actor can donate tokens directly to the pool's sovereign `AccountId` (obtainable via the public `PoolLocator`) to distort `reserve1`/`reserve2` at will, without any transaction going through `add_liquidity`/`swap_*` and without minting/burning any LP tokens. This corrupts:
- the `lp_token_amount` computed for the next legitimate `add_liquidity` caller (their share of the pool can be diluted far below their fair proportional deposit because `side1`/`side2` in `mul_div(amount, total_supply, reserve)` round down against an artificially inflated `reserve`), and
- every downstream price quote (`quote_price_exact_tokens_for_tokens`, `quote_price_tokens_for_exact_tokens`, `get_amount_in`, `get_amount_out`) that other pallets (notably fee payment) rely on as ground truth.

### Impact Explanation
This maps to the "public underpriced work" and "value conservation" impact categories: reserve/price corruption via a costless, permissionless donation directly undermines the AMM's core invariant that reserves reflect actual liquidity-provider-backed funds. Any consumer that treats `get_reserves`/`quote_price_*` as trustworthy (fee-charging via `pallet-asset-conversion-tx-payment`, third-party swap routing, wallets) can be misled into under/over-pricing, and legitimate LPs calling `add_liquidity` right after a donation can have their minted LP-token share computed against a corrupted denominator, producing an incorrect (diluted) ownership stake relative to the value they deposited.

### Likelihood Explanation
The precondition (transfer tokens to a known, publicly derivable sovereign account) requires no privileged origin, no relayer/validator collusion, and no governance action - it is a plain signed `transfer` extrinsic available to any account holding the relevant asset. The pool account address is deterministically derivable via `PoolLocator::address`/`pool_address`, which is a public, non-privileged API. The attack is cheap (bounded only by the amount of tokens the attacker is willing to donate) and can be executed at will since reserves are read live at the time of each call.

### Recommendation
Do not use the pool account's raw total balance as the trusted reserve for pricing and LP-mint math. Maintain an explicit, pallet-tracked reserve value per pool (updated only through `add_liquidity`/`remove_liquidity`/`swap_*` accounting), analogous to Uniswap V2's cached `reserve0`/`reserve1` that are synced explicitly rather than read live from `balanceOf`. Alternatively, reconcile any balance in excess of the tracked reserve as a "skim"/protocol-owned surplus that is swept out and never folded into LP-share or price computations - the same remediation direction recommended in the source report (separate protocol/foreign funds from the balance used for share accounting).

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)`, deriving `pool_account` via `T::PoolLocator::address`. [8](#0-7) 
2. Attacker performs a plain `transfer` of `asset2` (or `asset1`) directly to `pool_account`, without calling `add_liquidity` - no LP tokens are minted, `total_supply` stays `0`.
3. A legitimate LP then calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)`. Because `reserve1`/`reserve2` are computed from `get_balance` (raw balance, now including the attacker's donation), the "one side zero" branch or the proportional `mul_div` branch in `do_add_liquidity` computes `lp_token_amount` against the polluted reserve, producing an LP-token allocation that does not match the actual value the honest LP is contributing relative to the pool's true (attacker-funded) balance. [9](#0-8) 
4. Any subsequent `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` call (including those used internally by `pallet-asset-conversion-tx-payment` for fee pricing) returns a value computed from the same polluted `get_reserves` result, propagating the corrupted price to fee charging and swap routing. [10](#0-9) 

Note: I was not able to fully verify within the available searches whether FRAME's default transactional-dispatch rollback fully neutralizes fund-loss in every downstream scenario (e.g., whether `Err` from `ensure!(lp_token_amount > MintMinLiquidity)` always reverts prior transfers in every runtime configuration); this would need to be confirmed with an actual test run in a full Devin session before treating this as a guaranteed direct fund-loss exploit versus a price/share-corruption issue.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-877)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

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

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());

			let lp_token_amount: T::Balance;
			if total_supply.is_zero() {
				lp_token_amount = Self::calc_lp_amount_for_zero_supply(&amount1, &amount2)?;
				T::PoolAssets::mint_into(
					pool.lp_token.clone(),
					&pool_account,
					T::MintMinLiquidity::get(),
				)?;
			} else {
				let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
				let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
				lp_token_amount = side1.min(side2);
			}

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1547)
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

**File:** prdoc/pr_12408.prdoc (L1-11)
```text
title: 'fix(asset-conversion): use full balances for pool prices'
doc:
- audience: Runtime Dev
  description: |
    `pallet-asset-conversion` now reads full pool account balances when calculating
    pool prices and liquidity amounts. Previously, these calculations used reducible
    balances, which could understate pool reserves when protected funds or unrelated
    non-sufficient assets were held in the pool account.
crates:
- name: pallet-asset-conversion
  bump: patch
```
