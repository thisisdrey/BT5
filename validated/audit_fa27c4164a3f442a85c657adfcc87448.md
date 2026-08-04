### Title
Direct token donation to an `pallet-asset-conversion` pool account inflates reserves and lets an early LP steal a later provider's contribution - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes LP-share issuance from the *live token balance* of the pool account (`get_balance`/`get_reserves`) rather than from an internally protected accounting value that only changes when `add_liquidity`/`remove_liquidity` execute. Because the pool account is a normal account, anyone can transfer assets directly to it, inflating the "reserve" the share-mint formula divides by without minting any corresponding LP tokens. This is the same broken invariant as the BPLP report: a share-issuing pool that can be "credited" with value outside of the deposit path, letting an attacker who holds the (near-)totality of existing shares capture value a legitimate depositor contributes afterward.

### Finding Description
`do_add_liquidity` reads the pool reserves straight from the pool account's balances: [1](#0-0) 

then, once `total_supply` is non-zero, issues LP tokens proportionally to those reserves: [2](#0-1) 

`reserve1`/`reserve2` here are the pool account's actual on-chain asset balances (`Self::get_balance`), not a value that is only mutated inside `do_add_liquidity`/`do_remove_liquidity`. Any account can transfer `asset1`/`asset2` directly to the pool account (a normal, non-privileged account address) using the standard `assets`/`balances` transfer extrinsics, exactly like BPLP's unrestricted `credit()` let anyone add ether to `Vaults[vaultId].amtETH` without minting shares.

Attack pattern, mirroring the report's Alice/Bob sequence:
1. Attacker creates the pool and adds the smallest viable initial liquidity, becoming (almost) the sole LP-token holder — total supply is `sqrt(amount1*amount2) - MintMinLiquidity`, with `MintMinLiquidity` permanently minted to the pool account itself: [3](#0-2) [4](#0-3) 
2. Attacker then directly transfers a large amount of `asset1`/`asset2` to the pool account (a plain transfer, not `add_liquidity`), inflating `reserve1`/`reserve2` without changing `total_supply`.
3. A legitimate LP calls `add_liquidity` with amounts computed against the honest ratio (via `Self::quote`), but `lp_token_amount = min(side1, side2)` where `side_i = amount_i * total_supply / reserve_i` — the denominator is now inflated by the attacker's donation, so the victim receives far fewer LP tokens than their contribution is worth.
4. When the attacker calls `remove_liquidity`, `amount_i = lp_redeem_amount * reserve_i / total_supply` — the attacker's (still-dominant) share of `total_supply` now entitles them to a proportionally larger slice of the reserve that includes the victim's under-compensated deposit: [5](#0-4) 

The root cause is identical to the BPLP bug: shares are minted as `amount * supply / balance`, and `balance` can be moved by a party other than the depositor through a channel (BPLP's `credit()`, here a plain asset transfer to the pool account) that does not mint proportional shares.

### Impact Explanation
This lets an unprivileged actor (the first/dominant LP) permanently capture value that later liquidity providers deposit into a public asset-conversion pool, i.e. theft/mis-settlement of user funds through a public entry point (`add_liquidity`) without needing any admin, governance, validator, or off-chain privilege — matching the "theft or unbacked mint" and "conserve value / settle exactly once to the rightful beneficiary" impact categories.

### Likelihood Explanation
Likelihood is bounded by two mitigations already present that partially offset (but do not eliminate) the attack surface:
- `MintMinLiquidity` is permanently locked to the pool account on first mint, diluting a would-be attacker's exact share of `total_supply` (unlike BPLP, where the attacker could get literally 100% of the shares with 1 wei).
- `ensure!(lp_token_amount > T::MintMinLiquidity::get())` reverts add_liquidity outright if the victim's minted amount rounds too low, which can turn a subtle theft into a denial-of-service (griefing) for small deposits rather than silent value transfer.

However, for a sufficiently large donation relative to a moderate victim deposit, `lp_token_amount` can still be non-zero yet disproportionately small (rather than exactly zero), so the transaction succeeds while transferring economic value from the victim to the attacker — the described theft path is not eliminated by these guards, only bounded.

### Recommendation
Track pool reserves as pallet storage state updated only inside `do_add_liquidity`/`do_remove_liquidity`/`swap`, instead of trusting the pool account's live asset balance for share-minting math; or require `add_liquidity` to reconcile/absorb any unaccounted balance into the depositor's own minted shares (donation-safe accounting), and add regression tests simulating a bare `Assets::transfer` donation to the pool account followed by `add_liquidity`/`remove_liquidity` to confirm shares stay proportional to true contributions.

### Proof of Concept
Conceptual reproduction using existing pallet tests as scaffolding:
1. `create_pool(asset1, asset2)`, then attacker calls `add_liquidity` with a small `amount1_desired`/`amount2_desired` (e.g. values just above `MintMinLiquidity` threshold), receiving nearly all outstanding LP tokens.
2. Attacker calls `Assets::transfer`/`Balances::transfer` directly to the pool account address (obtained via `T::PoolLocator::address`) for a large amount of `asset1` and `asset2`, bypassing `add_liquidity` entirely (reserves inflate, `total_issuance(lp_token)` unchanged) — see reserve read at [1](#0-0) .
3. Victim calls `add_liquidity` with normal-sized amounts matching the (now inflated) quoted ratio; observe `lp_token_amount` minted to victim is disproportionately small relative to the value transferred, per the `side1.min(side2)` calculation at [6](#0-5) .
4. Attacker calls `remove_liquidity` for their LP tokens and receives back more value (including a slice of the victim's deposit) than they ever contributed, per the withdrawal formula at [5](#0-4) .

Note: I was not able to execute this scenario against the pallet's test harness (`substrate/frame/asset-conversion/src/mock.rs`/`tests.rs`) in this session to obtain concrete numeric before/after balances; the analysis above is derived directly from the reserve-read and share-mint/redeem formulas cited. A Devin session with repo execution access would be needed to run a concrete `#[test]` reproducing exact numbers.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L915-920)
```rust
			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1353-1368)
```rust
		pub(super) fn calc_lp_amount_for_zero_supply(
			amount1: &T::Balance,
			amount2: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			let amount1 = T::HigherPrecisionBalance::from(*amount1);
			let amount2 = T::HigherPrecisionBalance::from(*amount2);

			let result = amount1
				.checked_mul(&amount2)
				.ok_or(Error::<T>::Overflow)?
				.integer_sqrt()
				.checked_sub(&T::MintMinLiquidity::get().into())
				.ok_or(Error::<T>::InsufficientLiquidityMinted)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
		}
```
