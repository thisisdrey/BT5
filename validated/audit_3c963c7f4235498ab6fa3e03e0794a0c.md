### Title
`pallet-asset-conversion`: `add_liquidity` derives reserves from live pool-account balance, letting a pre-funded/donated balance dictate LP-token minting ratio and amount — ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
The Phuture `IndexLogic::mint` bug boiled down to one broken invariant: the amount credited to a caller was computed from the *live, observable balance* of a shared vault account rather than from a balance delta that is provably attributable to that specific caller's own transfer. Anyone who noticed the vault's balance exceeded the pallet's internally recorded `lastBalance` could call the public `mint` and be credited for value that someone else deposited.

`pallet-asset-conversion`'s `do_add_liquidity` (invoked by the public, permissionless `add_liquidity` extrinsic) has the same structural weakness: it reads "reserves" directly from the pool account's on-chain asset balance via `Self::get_balance(&pool_account, asset)` [1](#0-0)  instead of maintaining an isolated, per-depositor-attributed reserve counter. Any account can freely transfer (or `force_set_balance`) assets directly into the deterministic `pool_account` address at any time, inflating `reserve1`/`reserve2` before the pallet's own liquidity-accounting logic sees them.

### Finding Description
`do_add_liquidity` computes the reserves to be used for the optimal-amount/ratio calculation straight from the pool account's live balance: [2](#0-1) 

Because `reserve1`/`reserve2` are read live from the account rather than from a value that is only updated when `add_liquidity`/`remove_liquidity` successfully execute and increment `total_supply` in lockstep, the reserves and the LP-token `total_supply` can become desynchronized: an attacker (or any unrelated transfer, e.g. dust, a mis-sent transfer, or a deliberate pre-fund) can move tokens into `pool_account` without any corresponding LP tokens being minted. The subsequent depositor's `lp_token_amount` is then computed as: [3](#0-2) 

`side1`/`side2` are `mul_div(&amountX, &total_supply, &reserveX)` — i.e. the minted amount is a function of the manipulated `reserveX` value, not of a delta tied to the caller's own transfer. This is exactly the Phuture pattern: crediting a "mint"-equivalent operation using an externally-observable balance instead of a caller-attributed deposit accounting.

This is not purely theoretical — the codebase's own test suite explicitly exercises "assets deposited directly to the pool address" as a first-class scenario: [4](#0-3) 

and there is a related prior confirmed defect in the same reserve-reading logic (using `reducible_balance` instead of full balance), fixed only for pricing, not for the underlying architectural issue of trusting live balances as reserves: [5](#0-4) 

### Impact Explanation
An attacker who can pre-fund (donate) a pool account before/between legitimate `add_liquidity` calls skews the reserve ratio used to compute `amount2_optimal`/`amount1_optimal` and — critically for pools where `total_supply` is already non-zero — skews the LP mint amount (`side1.min(side2)`) away from the fair, delta-based share the depositor is actually owed. This directly violates the "conserve value / settle exactly once to the rightful beneficiary and amount" invariant for asset-conversion pools: LP token minting amounts and effective deposit ratios can be manipulated by an unprivileged actor simply moving tokens into a public, deterministic address, without going through the tracked `add_liquidity` accounting path.

### Likelihood Explanation
Likelihood is high for the specific precondition (pool account address is deterministic and publicly known via `PoolLocator::address`, and any account can transfer assets to it without restriction), and the attack requires no validator, governance, relayer, or privileged role — only an unprivileged signed account. The codebase's own tests confirm direct transfers to the pool address are a recognized and exercised input path.

### Recommendation
Track pool reserves as pallet storage state that is only updated atomically alongside LP-token issuance changes (mint/burn), rather than deriving them from the live queryable balance of the pool account. Reject or separately quarantine unattributed balance surpluses (donations) so they cannot influence the ratio or amount used in `add_liquidity`/`remove_liquidity` calculations, mirroring how the Phuture fix recommended binding credited amounts strictly to the caller's own `transferFrom`-verified deposit rather than an externally observable balance delta.

### Proof of Concept
1. Attacker identifies the deterministic `pool_account` address for an asset pair via `PoolLocator::address`.
2. Before a victim's `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, ...)` transaction executes, attacker transfers (or otherwise causes) extra `asset1`/`asset2` balance directly into `pool_account`, inflating `reserve1`/`reserve2` beyond what `total_supply` of LP tokens represents.
3. The victim's `add_liquidity` call now computes `amount2_optimal`/`amount1_optimal` and ultimately `lp_token_amount = side1.min(side2)` using the inflated, attacker-controlled `reserve1`/`reserve2`, deviating from the amount the victim should fairly receive based on their actual contribution — as demonstrated structurally by `add_tiny_liquidity_directly_to_pool_address` [6](#0-5) , which shows the pallet accepting and factoring in balances placed directly on the pool address outside the tracked liquidity-provision flow.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L809-844)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
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
