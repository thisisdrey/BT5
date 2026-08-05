Based on my investigation, `pallet-asset-conversion` computes pool "reserves" (used for pricing, swap output/input calculations, and LP-share issuance) directly from the pool account's live token balance rather than from an internally tracked accounting value. This is confirmed by `prdoc/pr_12408.prdoc`, which documents that the pallet "reads full pool account balances when calculating pool prices and liquidity amounts," and by `quote_price_tokens_for_exact_tokens` in `substrate/frame/asset-conversion/src/lib.rs:1564-1604`, which calls `Self::get_reserves(...)` and `T::Assets::reducible_balance(asset2, &pool_account, ...)` to derive swap-relevant balances directly from the pool account.

### Title
Pool reserves derived from live pool-account balance allow donation-based price/LP-share manipulation - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
`pallet-asset-conversion` treats the pool account's on-chain token balance as ground truth for reserves (`get_reserves`), instead of maintaining reserves as pallet-internal state updated only on `add_liquidity`/`remove_liquidity`/`swap`. Anyone can transfer (`donate`) tokens directly to a pool account outside of any pallet extrinsic, which silently and immediately changes the value returned by `get_reserves`/`quote_price_*` and the `(reserve_in, reserve_out)` figures used in `get_amount_out`/`get_amount_in`, exactly the "sync cash from direct balance" pattern flagged in the external report (`InitCore`'s `syncCash` trusting `IERC20(underlyingToken).balanceOf(address(this))`). [1](#0-0) [2](#0-1) 

### Finding Description
`Self::get_reserves(asset1, asset2)` and swap-quote helpers pull the current balances of the pool account for `asset1`/`asset2` and use these directly as reserves in constant-product math (`get_amount_out`, `get_amount_in`, `quote`) as seen in `balance_path_from_amount_in` and `quote_price_tokens_for_exact_tokens`. [3](#0-2) [4](#0-3) 

Because these balances are the raw, permissionless `T::Assets`/`T::Currency` account balances of the pool address (a deterministically derived, publicly known account), any account can call a plain `transfer` (no special extrinsic, no admin, no privileged action) to move extra units of `asset1` or `asset2` into the pool account. The very next read of `get_reserves` (triggered by any subsequent `swap`, `add_liquidity`, or off-chain price RPC via `quote_price_tokens_for_exact_tokens`) will reflect the donated amount as part of the reserve, exactly like `InitCore.syncCash()` trusting `balanceOf(address(this))` after an attacker-controlled deposit. This is the same broken invariant as the external report: reserve/cash accounting is derived from `balanceOf` rather than from pallet-tracked deposits, so a donation instantly and permanently (until consumed) skews the price curve and LP-share-minting ratio used by `do_add_liquidity`, which issues LP tokens proportional to the (potentially donation-inflated) reserve ratio. [5](#0-4) 

The prdoc for the historical fix (`pr_12408`) confirms the pallet's design intentionally reads the pool account's *full* balance (not merely "reducible") for pricing — i.e., it explicitly widens the surface exposed to a direct-donation transfer, since even non-reducible/protected balances are folded into "reserves." [2](#0-1) 

### Impact Explanation
An attacker can skew swap pricing and LP-share issuance ratios by donating tokens to a pool account: this can (a) let the attacker mint disproportionate LP shares by calling `add_liquidity` right after a self-donation, then withdraw a disproportionate share of the *other* asset via `remove_liquidity`, or (b) manipulate the spot price reported by `quote_price_tokens_for_exact_tokens`, which is consumed by governance and other pallets (e.g., as a fee-conversion price oracle in `asset-hub` runtimes) to misprice extrinsic fees or asset conversions, causing fund loss for other users of the pool or the fee payer, matching the "theft or unbacked mint" and "public underpriced work" impact categories.

### Likelihood Explanation
Likelihood is moderate: the pool account address is deterministic and publicly derivable (`T::PoolLocator::pool_address`), and the donation requires only a standard, unprivileged `transfer` call plus a follow-up `add_liquidity`/`remove_liquidity`/swap call — no admin, governance, relayer, or validator collusion is needed. The main constraint is the attacker must be willing to lose the donated principal to the pool unless they can immediately recapture a larger proportional share via `add_liquidity`, which is a standard sandwich-style AMM donation attack.

### Recommendation
Track pool reserves as pallet-internal storage (e.g., a `PoolReserves` map updated atomically inside `do_add_liquidity`, `do_remove_liquidity`, and swap execution) instead of re-deriving reserves from `T::Assets::balance`/`T::Currency::free_balance` of the pool account on every read. If live-balance reads must remain for defensive reconciliation, clamp any increase to only be recognized through pallet-controlled deposit paths, never through raw external transfers.

### Proof of Concept
1. Attacker identifies the deterministic pool account for `(asset1, asset2)` via `T::PoolLocator::pool_address`.
2. Attacker calls a plain `Assets::transfer` (or native `Balances::transfer`) sending a large amount of `asset1` directly to the pool account — no pallet extrinsic guard prevents this.
3. Attacker immediately calls `AssetConversion::add_liquidity` with a small `asset2` amount; because `get_reserves` now reports the donation-inflated `asset1` balance, the LP-share-minting formula in `do_add_liquidity` issues the attacker a disproportionately large share of LP tokens relative to genuine liquidity contributed.
4. Attacker calls `remove_liquidity` to redeem the LP tokens, withdrawing a disproportionate amount of `asset2` (and any of `asset1` contributed by other liquidity providers), realizing profit from the donation-induced reserve skew — directly analogous to inflating `LendingPool.cash` via `syncCash()` after a token donation in the external report. [5](#0-4)

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1318-1341)
```rust
		/// Following an amount into a `path`, get the corresponding amounts out.
		pub(crate) fn balance_path_from_amount_in(
			amount_in: T::Balance,
			path: Vec<T::AssetKind>,
		) -> Result<BalancePath<T>, DispatchError> {
			let mut balance_path: BalancePath<T> = Vec::with_capacity(path.len());
			let mut amount_out: T::Balance = amount_in;

			let mut iter = path.into_iter().peekable();
			while let Some(asset1) = iter.next() {
				let asset2 = match iter.peek() {
					Some(a) => a,
					None => {
						balance_path.push((asset1, amount_out));
						break;
					},
				};
				let fee = Self::pool_fee_for(&asset1, asset2)?;
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset1, amount_out));
				amount_out = Self::get_amount_out(fee, &amount_out, &reserve_in, &reserve_out)?;
			}
			Ok(balance_path)
		}
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
