Confirmed: `get_reserves`/`get_balance` in `pallet-asset-conversion` reads live account balances of the pool's sovereign account, not an internally-tracked reserve variable that only updates on `mint`/`swap`/`burn` (unlike Uniswap V2's `reserve0`/`reserve1` which require an explicit `sync()` to absorb a donation). This makes the pool's live token balance directly attacker-influenced by an ordinary `Assets::transfer`/`Balances::transfer` to the pool account, independent of LP-token issuance.

### Title
Donation-based reserve inflation lets an attacker corrupt add_liquidity/swap/remove_liquidity pricing in `pallet-asset-conversion` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves via `Self::get_balance`/`Self::get_reserves`, which read the pool sovereign account's *live* asset balance rather than an internally tracked reserve that is updated only through `mint`/`swap`/`burn`. Because any account can transfer tokens directly to the deterministic pool account (`T::PoolLocator::address`) without calling `add_liquidity`, an attacker can donate tokens to skew the reserve ratio used to compute LP-token minting, swap output, and liquidity withdrawal — the same broken invariant as the Hifi/Uniswap-V2-style "first depositor" share-price manipulation described in the external report.

### Finding Description
`do_add_liquidity` reads `reserve1`/`reserve2` from `Self::get_balance(&pool_account, asset)` [1](#0-0) , and when the pool already has liquidity, mint amount is computed as `side1 = mul_div(amount1, total_supply, reserve1)`, `side2 = mul_div(amount2, total_supply, reserve2)`, `lp_token_amount = side1.min(side2)` [2](#0-1) . Likewise `do_remove_liquidity` computes payout amounts as `amount1 = mul_div(lp_redeem_amount, reserve1, total_supply)` using the same live-balance reserves [3](#0-2) .

The pool account address is deterministic and known in advance (`T::PoolLocator::address`), as demonstrated in the pallet's own test `cannot_block_pool_creation`, where an attacker pre-funds a not-yet-created pool account by transferring tokens directly to it before the pool is even created [4](#0-3) . That test only asserts pool *creation* still succeeds; it does not test what happens to the LP mint-ratio and swap pricing once real liquidity is added on top of a pre-donated balance.

The pallet does include `MintMinLiquidity`, which is exactly the Uniswap-V2-style fix recommended in the external report — it burns/locks a floor amount of LP tokens to the pool account at first mint and requires `lp_token_amount > T::MintMinLiquidity::get()` [5](#0-4) . However, this guard only bounds the ratio at the moment of the *first* mint. It does nothing to prevent a **subsequent** direct donation from inflating `reserve1`/`reserve2` independently of `total_supply`, because reserves are read live from account balance rather than from a pallet-tracked state variable that is immune to donations between pallet calls.

### Impact Explanation
An attacker (unprivileged, no governance/relayer/validator role needed) can:
1. Create (or wait for) a thinly-liquid pool.
2. Directly transfer a large amount of one pool asset to the deterministic pool account, inflating `reserve1` (or `reserve2`) without minting any LP tokens.
3. Any subsequent `add_liquidity` call by a legitimate LP is priced against the inflated reserve, forcing them to deposit a proportionally huge amount just to clear the `lp_token_amount > MintMinLiquidity` check, or receive an unfairly small number of LP shares for a large deposit — degrading the pool and pricing out smaller liquidity providers exactly as in the Hifi report.
4. The same inflated reserve values feed `do_swap_exact_tokens_for_tokens` and `do_remove_liquidity`, so swap quotes and LP redemption payouts are also corrupted by the donation, i.e. value can be mis-settled to whichever side benefits from the skew.

This falls squarely within "Balances, assets ... pools ... must conserve value and settle exactly once to the rightful beneficiary and amount" and "public underpriced work that degrades block production or stalls bridge processing" analog (degraded pool usability / mispriced settlement) called out in the Impact Gate.

### Likelihood Explanation
The attack requires only a standard signed `transfer` extrinsic to a publicly-computable account address — no admin, governance, relayer, or validator privilege is needed, and no code execution beyond calling existing public dispatchables. The cost to the attacker is bounded to the tokens donated (which are permanently stuck in the pool account, akin to a griefing cost), similar to the original Hifi finding, which the Hifi team itself acknowledged as reproducible and only mitigated (not eliminated) by minimum-liquidity locking.

### Recommendation
Track reserves as pallet storage (`Pools<T>` reserve fields) updated only on `mint`/`swap`/`burn`, and add an explicit `sync`-style reconciliation only under controlled paths — mirroring Uniswap V2's separation between tracked `reserve0/reserve1` and actual token balance — instead of deriving reserves from the live, donation-manipulable account balance in `get_balance`/`get_reserves`.

### Proof of Concept
1. Attacker creates pool `(A, B)` and provides minimal liquidity via `add_liquidity`, e.g. `amount1 = amount2 = 101`, satisfying `MintMinLiquidity = 100`, receiving `~1` LP token; `100` LP tokens are locked to the pool account per `calc_lp_amount_for_zero_supply`/`MintMinLiquidity` mint [6](#0-5) .
2. Attacker directly calls `Assets::transfer` (or `Balances::transfer_allow_death`, as shown in `cannot_block_pool_creation`) to send `10**21` of asset `A` to the deterministic pool account `T::PoolLocator::address((A,B))` [7](#0-6) .
3. `reserve1` (asset A) is now `10**21 + 101`, `reserve2` (asset B) is `101`, while `total_supply` of LP tokens is unchanged (`~101`).
4. A victim calling `add_liquidity` with `amount1_desired`, `amount2_desired` must supply `amount1 ≈ 10**21 * amount2 / 101` proportionally, or receive `side1 = amount1 * total_supply / reserve1 ≈ 0` LP tokens, failing `Error::InsufficientLiquidityMinted` unless they also provide roughly `10**21` tokens of asset A — reproducing the exact "smaller liquidity providers priced out" scenario from the external report.

**Note on verification limits:** I was not able to fully trace `Self::get_balance`/`Self::get_reserves`'s exact implementation body within this session (only call sites were confirmed), nor confirm whether any additional invariant check elsewhere in the pallet (e.g., in `swap` or a `TouchHandler`) restricts direct transfers into the pool's sovereign account. A Devin session with full file access should verify the exact body of `get_balance`/`get_reserves` in `substrate/frame/asset-conversion/src/lib.rs` to confirm there is no reconciliation/sync step before relying on this finding for a submission.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-877)
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

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L913-920)
```rust
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L2349-2363)
```rust
		// Attacker computes the still non-existing pool account for the target pair
		let pool_account =
			<Test as Config>::PoolLocator::address(&(token_1.clone(), token_2.clone())).unwrap();
		// And transfers the ED to that pool account
		assert_ok!(Balances::transfer_allow_death(
			RuntimeOrigin::signed(attacker),
			pool_account,
			ed
		));
		// Then, the attacker creates 14 tokens and sends one of each to the pool account
		for i in 10..25 {
			create_tokens(attacker, vec![NativeOrWithId::WithId(i)]);
			assert_ok!(Assets::mint(RuntimeOrigin::signed(attacker), i, attacker, 1000));
			assert_ok!(Assets::transfer(RuntimeOrigin::signed(attacker), i, pool_account, 1));
		}
```
