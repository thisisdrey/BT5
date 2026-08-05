### Title
Reserve-inflation share dilution in `pallet-asset-conversion` liquidity minting via direct token donation - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves as the *live* token balance of the pool account (`Self::get_balance(&pool_account, asset)`) rather than an internally tracked, mint-gated accounting value. `do_add_liquidity` uses these live balances directly as `reserve1`/`reserve2` when computing how many LP tokens to mint for a new depositor. Since any unprivileged account can transfer `asset1`/`asset2` (native `Balances::transfer` or `pallet_assets::transfer`) directly to the pool account without calling `add_liquidity`, an attacker can inflate the apparent reserves relative to the LP token `total_supply`, causing subsequent depositors' minted-share calculation to round far below their fair share — the same "inflate the exchange-rate denominator via raw balance donation" primitive described in the iToken report.

### Finding Description
In `do_add_liquidity`: [1](#0-0) 
`reserve1`/`reserve2` are read straight from the pool account's actual asset balance, not from a pallet-maintained reserve counter.

When `total_supply` (the LP token's `total_issuance`) is non-zero, new LP tokens are minted proportionally to these live reserves: [2](#0-1) 
`side1 = amount1 * total_supply / reserve1`, `side2 = amount2 * total_supply / reserve2`, and the minted amount is `min(side1, side2)`.

Because `reserve1`/`reserve2` are simply `T::Assets::balance(asset, &pool_account)`, an attacker can, at any time after pool creation, directly transfer (bypassing `add_liquidity` entirely) large amounts of `asset1` and/or `asset2` into the pool account. This raises `reserve1`/`reserve2` without minting any LP tokens (`total_supply` unchanged), which is mathematically identical to the iToken bug: an attacker manipulates the "assets-per-share" ratio by directly funding the pooled account, rather than through the accounting entrypoint that would keep shares and assets in lockstep.

A subsequent legitimate liquidity provider's `amount1`/`amount2` get divided by the now-inflated `reserve1`/`reserve2`, so `side1`/`side2` round down disproportionately, and they are minted far fewer LP tokens than their contribution is worth. The only guard is: [3](#0-2) 
which merely requires the minted amount exceed `MintMinLiquidity` (default 100) — it does not protect against *disproportionate* dilution, only against literally zero/negligible minting. The attacker, already holding pool shares (obtained cheaply before donating), can then call `remove_liquidity`, which also computes payout from live reserves: [4](#0-3) 
and withdraw a share of the pool that includes the victim's newly deposited underpriced contribution.

This differs from the ordinary AMM "donation just benefits existing LPs proportionally" case (which is expected/benign) because it specifically targets the *minting math for new depositors*, exactly mirroring the iToken/ERC4626 inflation-attack class: the exchange-rate denominator (reserves) is derived from a balance an unprivileged party can directly manipulate, independent of the numerator (LP total supply) that only the pallet controls.

### Impact Explanation
This breaks the "balances, assets, ... conserve value and settle exactly once to the rightful beneficiary and amount" invariant for `pallet-asset-conversion`, an Asset Hub / parachain pallet used in `paritytech/polkadot-sdk` runtimes (e.g., `substrate/bin/node/runtime/src/lib.rs`, Asset Hub Westend/Rococo runtimes). A liquidity provider can lose a large fraction of the assets they deposit into `add_liquidity`, with the loss transferred to whoever pre-positioned LP shares before donating. This is a real, unprivileged-attacker fund-theft/loss vector against public entry points (`add_liquidity`/`remove_liquidity`), not a governance, relayer, or peer-trust issue.

### Likelihood Explanation
The attack requires only:
1. An existing pool with low `total_supply` (e.g. right after `create_pool` + minimal `add_liquidity`), and
2. The attacker being able to transfer `asset1`/`asset2` to the deterministic, publicly-derivable pool account (`T::PoolLocator::address`), which is a normal, permissionless `transfer` call for both `Balances` and `pallet_assets`.

No admin, governance, validator, or off-chain relayer/prover involvement is needed, and no front-running is strictly required (the attacker can wait for any victim to add liquidity to a thin pool). The pool account address is derivable by anyone via `PoolLocator`, and thinly-liquid pools (freshly created, or after most LPs exit) are common in practice, making this readily exploitable whenever a pool's `total_supply` is low relative to potential donation size.

### Recommendation
Track `reserve1`/`reserve2` as pallet-internal state (updated only through `add_liquidity`/`remove_liquidity`/`swap_*`) instead of reading the pool account's live balance, or, at minimum, reconcile/cap the deposit calculation against the last known accounted reserve and reject `add_liquidity` calls where the observed balance diverges unexpectedly from the pallet's tracked value. Alternatively, mint LP shares using a formula that is robust to unaccounted balance increases (e.g., snapshotting reserves atomically with mint accounting, or requiring that untracked balance surpluses be swept/burned rather than counted toward `reserve1`/`reserve2` for share-minting purposes).

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` then `add_liquidity` with `amount1_desired = amount2_desired = L` just above the threshold needed to satisfy `lp_token_amount > MintMinLiquidity` (e.g. `L = 200` with `MintMinLiquidity = 100`), receiving `lp_token_amount ≈ sqrt(L*L) - MintMinLiquidity` LP tokens while `reserve1 = reserve2 = L`.
2. Attacker directly calls `pallet_assets::transfer` (or `Balances::transfer_keep_alive` for the native side) sending a large `D >> L` of `asset1` and `asset2` directly to the pool account address (obtained via `PoolLocator::address`). This raises `reserve1`, `reserve2` to `L + D` without changing `total_supply`.
3. Victim calls `add_liquidity` with a legitimate deposit `amount1_desired = amount2_desired = X` (X comparable to a normal contribution). `Self::do_add_liquidity` computes `side1 = X * total_supply / (L + D)`, `side2` similarly — a value much smaller than it would have been against the undonated reserve of `L`.
4. Victim ends up minted disproportionately few LP tokens for `X`, while the attacker's existing LP tokens now represent claim on `L + D + X` pooled assets.
5. Attacker calls `remove_liquidity` burning their LP tokens, redeeming (via `do_remove_liquidity`'s `mul_div(lp_redeem_amount, reserve, total_supply)`) an amount that includes a share of the victim's `X` deposit, realizing profit at the victim's expense.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-872)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L874-877)
```rust
			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L915-920)
```rust
			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```
