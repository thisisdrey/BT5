### Title
Direct token donation to a pool's sovereign account manipulates `do_add_liquidity`/`do_remove_liquidity` reserve calculations, causing depositors to lose value via rounding — ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`pallet-asset-conversion` computes pool reserves by reading the *live* on-chain balance of the pool's sovereign account rather than an internally tracked reserve value. Because any account can transfer tokens directly to that sovereign account (it's a normal `AccountId`, fully derivable via `PoolLocator::address`), an attacker can inflate `reserve1`/`reserve2` used by `do_add_liquidity` without minting any LP tokens for the donated amount. This mirrors the reported `BinPool::_updateBin` bug class: a manipulated ratio between "reserve" and "supply" that, combined with rounding-down share math, causes a legitimate depositor to receive far fewer LP shares than their contribution warrants, permanently losing value to existing/attacker-controlled shareholders.

### Finding Description
`get_balance`/`get_reserves` read the pool account's actual asset balance directly from the `Assets` implementation: [1](#0-0) 

These reserves feed directly into `do_add_liquidity`'s share-minting math: [2](#0-1) [3](#0-2) 

`lp_token_amount = min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)`, using integer division (`mul_div`) that rounds down: [4](#0-3) 

Because `reserve1`/`reserve2` are read live from the pool account's balance rather than from a state variable that only changes through `do_add_liquidity`/`do_remove_liquidity`/swap accounting, an attacker can call a plain `Assets::transfer` (or `Balances::transfer`) directly to the pool's sovereign account — computed off-chain via `PoolLocator::address` — to inflate the reserve without affecting `total_supply` of LP tokens. The existing repo test `cannot_block_pool_creation` at [5](#0-4) 
confirms that unsolicited direct transfers to the not-yet-created/created pool account are possible and already anticipated for a *different* attack (blocking pool creation via consumer refs) — it does **not** address the reserve-inflation/rounding issue analyzed here. The `quote()` function used to determine the optimal deposit ratio also directly consumes these manipulable reserves: [6](#0-5) 

Once reserves are artificially inflated relative to `total_supply`, any subsequent depositor's `side1`/`side2` values round down disproportionately, minting them materially fewer LP tokens than the fair share of pool value their deposit should represent. The only guard, `ensure!(lp_token_amount > T::MintMinLiquidity::get(), ...)`, only prevents a *complete* zero-mint; it does not prevent *severe underminting*, which is exactly the failure mode described in the external report (rounding-down composition-fee/share math silently costing the depositor most of their input value).

### Impact Explanation
A depositor who adds liquidity to a pool whose reserves have been inflated by a direct, unsolicited transfer receives LP tokens worth substantially less than the assets they contributed; the excess value accrues to whoever redeems LP tokens afterward (which can be the attacker if they hold pre-existing LP tokens, or simply a redistribution windfall to other LPs while the depositor suffers real fund loss). This is a direct violation of the "conserve value and settle exactly once to the rightful beneficiary and amount" pivot for balances/pools, reachable by any unprivileged account with no governance, validator, or relayer involvement — a pure public entrypoint interaction (plain asset transfer + `add_liquidity` call).

### Likelihood Explanation
The attack requires no special privilege, no collusion, and no race condition beyond ordinary transaction ordering (attacker sends the donation transfer before the victim's `add_liquidity` extrinsic executes, e.g., in the same or an earlier block). It is economically bounded only by the size of the donation the attacker is willing to risk and the size of the victim's deposit, which is the same cost/benefit profile as the classic ERC-4626/AMM "donation/inflation" attack referenced in the external report.

### Recommendation
Track pool reserves as pallet storage that is only mutated by `do_add_liquidity`, `do_remove_liquidity`, and swap execution, instead of reading the live balance of the pool's sovereign account. If live balances must be used for defensive reasons (e.g., to reconcile dust), clamp/ignore any balance surplus beyond the last recorded reserve when computing LP mint ratios, and/or require minted LP tokens to be bounded below by a value-conservation check comparing pre/post pool value per share.

### Proof of Concept
1. User A creates a pool for `(Native, AssetX)` and adds initial liquidity, receiving LP tokens (`total_supply > 0`, small reserves).
2. Attacker computes the pool's sovereign account via `PoolLocator::address` (public, deterministic) and calls `Assets::transfer`/`Balances::transfer_allow_death` to send a large amount of `Native` and/or `AssetX` directly into that account — this is an ordinary, permissionless transfer, not `add_liquidity`, so `total_supply` is unaffected.
3. Victim calls `add_liquidity` with a legitimate `amount1_desired`/`amount2_desired`. Inside `do_add_liquidity`, `reserve1 = get_balance(pool_account, asset1)` and `reserve2 = get_balance(pool_account, asset2)` now include the attacker's donation.
4. `side1 = mul_div(amount1, total_supply, reserve1)` and `side2 = mul_div(amount2, total_supply, reserve2)` are both rounded down against the inflated denominator, so `lp_token_amount = min(side1, side2)` is far smaller than the victim's fair share, while their tokens are already transferred into the pool at step 3 (`T::Assets::transfer` calls precede the LP-mint check).
5. Victim ends up owning a disproportionately small claim on the pool relative to their contribution, permanently losing the difference to the rest of the pool's LP-token holders (including the attacker if they pre-acquired LP tokens).

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-820)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());

			let amount1: T::Balance;
			let amount2: T::Balance;
			if reserve1.is_zero() || reserve2.is_zero() {
				amount1 = amount1_desired;
				amount2 = amount2_desired;
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1343-1351)
```rust
		/// Calculates the optimal amount from the reserves.
		pub fn quote(
			amount: &T::Balance,
			reserve1: &T::Balance,
			reserve2: &T::Balance,
		) -> Result<T::Balance, Error<T>> {
			// (amount * reserve2) / reserve1
			Self::mul_div(amount, reserve2, reserve1)
		}
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
