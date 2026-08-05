This confirms the mechanism: `do_remove_liquidity` computes `amount1 = mul_div(lp_redeem_amount, reserve1, total_supply)` using the same live-balance-derived `reserve1`/`reserve2` from `get_reserves` [1](#0-0) , so an attacker's tiny LP share redeems a proportional cut of whatever balance sits in the pool account at redemption time, including a victim's diluted deposit.

Audit Report

## Title
LP share dilution via donation-inflation of pool reserves in `pallet-asset-conversion::add_liquidity` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`pallet-asset-conversion` computes pool reserves by directly reading the pool sovereign account's live asset balance via `get_balance`/`get_reserves`, rather than maintaining an internally-tracked reserve that advances only through the pallet's own extrinsics. Because the pool account is an ordinary, publicly-transferable `AccountId` derived deterministically via `PoolLocator::address`, an unprivileged attacker can donate tokens directly to it to inflate the reserves used in `do_add_liquidity`'s LP-mint formula, causing a subsequent depositor's `add_liquidity` call to mint a disproportionately small LP share while their full deposit enters the pool balance; the attacker then redeems their (small) LP share via `do_remove_liquidity`, extracting a share of the pool inflated by the victim's contribution.

## Finding Description
`do_add_liquidity` reads `reserve1`/`reserve2` straight from the pool account's balance: [2](#0-1) 

and mints LP tokens as `min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)` when `total_supply` is nonzero: [3](#0-2) 

`get_reserves`, used by `do_remove_liquidity` and the public price-quote functions, derives reserves the same way: [4](#0-3) 

`do_remove_liquidity` then redeems `amount1 = mul_div(lp_redeem_amount, reserve1, total_supply)` using this live balance-derived reserve, so any tokens sitting in the pool account — donated or not — are distributed proportionally to LP-token holders: [1](#0-0) 

The only guard against a minimal/zero mint is `lp_token_amount > T::MintMinLiquidity::get()`, an absolute floor unrelated to the value actually contributed relative to the (attacker-inflated) reserve, so it does not prevent proportional dilution of a large deposit: [5](#0-4) 

The pallet's own test suite explicitly exercises third-party donations directly to the pool account and confirms `add_liquidity`/quoting still function normally against the inflated balance rather than rejecting or discounting it — e.g. `add_tiny_liquidity_directly_to_pool_address` force-sets a balance directly on the pool account before calling `add_liquidity`, and `cannot_block_pool_creation` transfers assets directly to a not-yet-created pool's deterministic address: [6](#0-5) [7](#0-6) 

These tests confirm the balance-based reserve mechanism is a known, intentional design characteristic of the pallet (extraneous/donated balances are tolerated and blended into reserves rather than rejected), not an unaddressed oversight, but they do not test or assert protection against the specific dilution/theft sequence: attacker mints minimal LP → donates large balance → victim's `add_liquidity` gets diluted → attacker's `remove_liquidity` extracts victim value. This sequence is a real, reachable exploit path requiring no privileged role, matching the classic "first depositor / donation" attack pattern common to constant-product AMMs (e.g., Uniswap V2) that lack a `sync`/skim mechanism or internally cached reserves decoupled from raw balance.

## Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for liquidity providers named in the Polkadot SDK Pivots. An unprivileged attacker can, through only the public `add_liquidity`/`remove_liquidity` extrinsics plus ordinary asset transfers to a derivable, unprivileged pool account, cause a subsequent liquidity provider to receive an LP-token amount disproportionately smaller than their real contribution, and later redeem the corresponding excess value themselves. The corrupted value is the pool's `reserve1`/`reserve2` (as read via `get_balance`/`get_reserves`) feeding both the LP-mint ratio in `do_add_liquidity` and the redemption ratio in `do_remove_liquidity`, and transiently the quotes returned by `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens`.

## Likelihood Explanation
The attack is fully self-contained: the attacker triggers both the donation and eventually collects the excess value themselves, requiring no privileged role, no compromised relayer/validator, and it is technically executable via ordinary `create_pool`, `add_liquidity`, plain asset `transfer`, and `remove_liquidity` calls (optionally batched atomically). However, realizing profit requires a subsequent, independent, unwitting victim to call `add_liquidity` on the specific pool while it is in the donation-inflated state — the attacker cannot force a victim to add liquidity at a chosen time, so the "attacker triggers the loss condition themselves" framing in the original report overstates certainty; profitability is conditional on a victim depositing into an already-manipulated pool (most exposed on freshly created, low-liquidity, low-cap pools) before the state normalizes via other market activity. This is consistent with a known first-depositor/donation AMM design gap rather than a code defect unique to an introduced regression, as evidenced by the pallet's existing tests deliberately exercising direct-donation scenarios without asserting dilution protection.

## Recommendation
Track pool reserves as pallet storage state mutated only by `add_liquidity`, `remove_liquidity`, and `swap` (Uniswap V2-style cached reserves updated at the end of each state-changing call) instead of recomputing them from the pool account's live balance in `get_balance`/`get_reserves`. Alternatively, bound the acceptable deviation between minted LP shares and actual contributed value, or add a `sync`/`skim`-style mechanism so donated balance cannot silently enter the `add_liquidity`/`remove_liquidity` ratio calculations in `do_add_liquidity` (`substrate/frame/asset-conversion/src/lib.rs:813-877`) and `do_remove_liquidity` (`substrate/frame/asset-conversion/src/lib.rs:913-920`).

## Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)`, then `add_liquidity(asset1, asset2, 1, 1, 1, 1, attacker)`, receiving minimal LP shares just above `MintMinLiquidity`.
2. Attacker transfers a large amount (e.g. `1_000_000`) of `asset1` and `asset2` directly to `PoolLocator::address(&pool_id)` via ordinary `Assets::transfer`, bypassing all `pallet-asset-conversion` extrinsics.
3. `get_reserves`/`get_balance` now report the inflated balances; `total_supply` of the LP token remains at its tiny original mint.
4. A victim calls `add_liquidity(asset1, asset2, 1_000_000, 1_000_000, 1, 1, victim)`; `lp_token_amount = min(amount1 * total_supply / reserve1, amount2 * total_supply / reserve2)` (`substrate/frame/asset-conversion/src/lib.rs:869-871`) rounds down to a value only marginally above `MintMinLiquidity`, while the victim's full deposit transfers into the pool account.
5. Attacker calls `remove_liquidity` with their original LP shares; `do_remove_liquidity`'s `mul_div(lp_redeem_amount, reserve1/2, total_supply)` (`substrate/frame/asset-conversion/src/lib.rs:919-920`) redeems a share of the pool inflated by the victim's deposit, extracting value disproportionate to the attacker's true contribution.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L868-877)
```rust
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

**File:** substrate/frame/asset-conversion/src/tests.rs (L469-522)
```rust
#[test]
fn add_tiny_liquidity_directly_to_pool_address() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);
		let token_3 = NativeOrWithId::WithId(3);

		create_tokens(user, vec![token_2.clone(), token_3.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_3.clone())
		));

		let ed = get_native_ed();
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), user, 10000 * 2 + ed));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 1000));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 3, user, 1000));

		// check we're still able to add the liquidity even when the pool already has some
		// token_1.clone()
		let pallet_account =
			<Test as Config>::PoolLocator::address(&(token_1.clone(), token_2.clone())).unwrap();
		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), pallet_account, 1000));

		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			10000,
			10,
			10000,
			10,
			user,
		));

		// check the same but for token_3.clone() (non-native token)
		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_3.clone()),
			10000,
			10,
			10000,
			10,
			user,
		));
	});
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
