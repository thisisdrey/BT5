### Title
Donation attack on `pallet-asset-conversion` pool reserves permanently blocks future liquidity provisioning - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion::do_add_liquidity` derives the pool's "reserves" by directly reading the live token balance of the pool's deterministic account (`Self::get_balance(&pool_account, asset)`), rather than from a value tracked and updated exclusively through the pallet's own accounting. Because the pool account address is deterministically derivable by anyone via `PoolLocator::address`/`pool_address`, an unprivileged attacker can transfer ("donate") tokens directly to that account outside of `add_liquidity`, inflating one side of the reserve pair. This distorts the LP-mint ratio calculation used for every subsequent depositor, and can drive the calculated LP-token amount to permanently round down to (or below) `MintMinLiquidity`, causing `add_liquidity` to revert with `InsufficientLiquidityMinted` for all future providers. This is the same broken invariant as the XDeFi report: a cheap, single-actor manipulation of a shared "ratio" variable (there: `_pointsPerUnit`; here: pool reserves) that a small self-funded local user can skew before honest users interact, causing legitimate future users' operations to fail/lock permanently.

### Finding Description
In `do_add_liquidity` [1](#0-0) , the pallet computes:

```
let reserve1 = Self::get_balance(&pool_account, asset1.clone());
let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

For non-first deposits, the number of LP tokens minted is:
```
let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
lp_token_amount = side1.min(side2);
``` [2](#0-1) 

`mul_div` performs `a * b / c` using a higher-precision integer type and rounds down on integer division [3](#0-2) . If an attacker inflates `reserve1` (or `reserve2`) by transferring a very large amount of the asset directly to the pool account — bypassing `add_liquidity` entirely — then for any realistic `amount1` a legitimate depositor can supply, `amount1 * total_supply` can be made smaller than `reserve1`, so `side1` rounds to `0`. Because the pallet then enforces `lp_token_amount > T::MintMinLiquidity::get()` [4](#0-3) , every future `add_liquidity` call on that pool reverts with `InsufficientLiquidityMinted`. The pool is therefore permanently unable to accept further liquidity, while the attacker's donated funds sit trapped/unreachable in the pool account (they are not represented by any tracked reserve variable and cannot be withdrawn via `remove_liquidity`, which is proportional to LP-token burn).

This is directly analogous to the XDeFi bug: there, a first user paid a trivial cost (1 wei bond) and then called the *public* `updateDistribution()` to blow up `_pointsPerUnit`, breaking subsequent `lock()`/`unlock()` arithmetic for all other users. Here, an unprivileged attacker pays only the cost of the donated tokens (which, unlike XDeFi's reward-accrual mechanism, do not even need to be "spent" — the tokens are merely moved to a known, permissionless-derivable account) to permanently corrupt the reserve ratio used by `do_add_liquidity`'s "public wrapper," causing legitimate value to be denied/wrong-sized (`side1`/`side2` computed with a corrupted denominator) and locking the pool from any future liquidity provisioning. Recent PR `pr_12408` (`fix(asset-conversion): use full balances for pool prices`) [5](#0-4)  shows the pallet deliberately switched to reading the *full* live balance (rather than a tracked/reducible one) for reserve/price calculations, which is precisely the design choice that makes this donation path effective — any token sent to the pool account, through any channel, is treated as "real" reserve for ratio math without validation that it entered through `add_liquidity`.

### Impact Explanation
This falls squarely within the "public underpriced work that degrades block production or stalls bridge processing" / "permanent user-fund or bridge-state lock" impact class: an unprivileged, single actor can permanently disable liquidity provisioning for a pool (a public AMM primitive used across the runtime, including for asset-based transaction fee payment via `pallet-asset-conversion-tx-payment`), and can also strand their own and subsequently mis-price other users' funds inside a pool account that cannot be recovered proportionally through `remove_liquidity`. It requires no admin/governance/validator/relayer compromise — a fully permissionless token transfer is sufficient.

### Likelihood Explanation
The attack primitive is cheap and requires only: (1) knowledge of the deterministic pool address (`PoolLocator::address`, a pure function of the asset pair, computable by anyone off-chain), and (2) the ability to transfer some quantity of one of the pool's two assets to that address — a completely permissionless operation. No special asset amount is even destroyed in the traditional sense; the funds are simply moved into an account that the pallet already treats as canonical "reserves." The existing regression test `cannot_block_pool_creation` [6](#0-5)  demonstrates the codebase is already aware attackers can pre-fund the deterministic pool account before pool creation, but it only asserts pool *creation* still succeeds — it does not test or guard against post-creation reserve inflation blocking subsequent `add_liquidity` calls.

### Recommendation
Track pool reserves as pallet storage state that is mutated only through `do_add_liquidity`/`do_remove_liquidity`/swap execution (mirroring the Uniswap V2 `sync()`-vs-`_reserve` separation, or simply ignoring un-credited balance deltas), rather than trusting the live queried balance of the pool account for LP-mint ratio math. At minimum, cap/clamp the influence of un-tracked balance increases on `mul_div`'s denominator, or require that `reserve` used in ratio math never exceed a bounded multiple of `total_supply`-scaled historical deposits, analogous to the nomination-pools `ok_to_join`/`ok_to_be_open` ratio-bound check [7](#0-6) .

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` and `add_liquidity` with the pallet-enforced minimum (`MintMinLiquidity` + 1), obtaining the pool's LP tokens, and note the deterministic `pool_account = PoolLocator::address(asset1, asset2)`.
2. Attacker transfers a very large amount of `asset1` directly to `pool_account` (a plain `Assets::transfer`/`Balances::transfer`, not via `add_liquidity`), inflating `reserve1 = Self::get_balance(pool_account, asset1)` far beyond `total_supply` of LP tokens times any realistic future deposit size.
3. Any subsequent legitimate user calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)`. Because `reserve1` is now enormous, `side1 = mul_div(amount1, total_supply, reserve1)` rounds to `0` for any feasible `amount1`, causing `lp_token_amount` (`= side1.min(side2)`) to fail the `lp_token_amount > MintMinLiquidity` check at `substrate/frame/asset-conversion/src/lib.rs:874-877`, reverting with `Error::InsufficientLiquidityMinted`.
4. The pool is now permanently unable to accept additional liquidity from any account, matching the "future users not able to lock()/unlock()" failure mode described in the source report, while the attacker's donated `asset1` remains stuck in the pool account, unrecoverable proportionally via `remove_liquidity` (which burns LP tokens against tracked reserves, not the inflated balance).

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L810-822)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
			} else {
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1370-1382)
```rust
		fn mul_div(a: &T::Balance, b: &T::Balance, c: &T::Balance) -> Result<T::Balance, Error<T>> {
			let a = T::HigherPrecisionBalance::from(*a);
			let b = T::HigherPrecisionBalance::from(*b);
			let c = T::HigherPrecisionBalance::from(*c);

			let result = a
				.checked_mul(&b)
				.ok_or(Error::<T>::Overflow)?
				.checked_div(&c)
				.ok_or(Error::<T>::Overflow)?;

			result.try_into().map_err(|_| Error::<T>::Overflow)
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

**File:** substrate/frame/asset-conversion/src/tests.rs (L2334-2391)
```rust
#[test]
fn cannot_block_pool_creation() {
	new_test_ext().execute_with(|| {
		// User 1 is the pool creator
		let user = 1;
		// User 2 is the attacker
		let attacker = 2;

		let ed = get_native_ed();
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), attacker, 10000 + ed));

		// The target pool the user wants to create is Native <=> WithId(2)
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

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

		// User can still create the pool
		create_tokens(user, vec![token_2.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));

		// User has to transfer one WithId(2) token to the pool account (otherwise add_liquidity
		// will fail with `AssetTwoDepositDidNotMeetMinimum`)
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), user, 10000 + ed));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 10000));
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(user), 2, pool_account, 1));

		// add_liquidity shouldn't fail because of the number of consumers
		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			10000,
			100,
			10000,
			10,
			user,
		));
	});
}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1189-1202)
```rust
		let points_to_balance_ratio_floor = self
			.points
			// We checked for zero above
			.div(bonded_balance);

		let max_points_to_balance = T::MaxPointsToBalance::get();

		// Pool points can inflate relative to balance, but only if the pool is slashed.
		// If we cap the ratio of points:balance so one cannot join a pool that has been slashed
		// by `max_points_to_balance`%, if not zero.
		ensure!(
			points_to_balance_ratio_floor < max_points_to_balance.into(),
			Error::<T>::OverflowRisk
		);
```
