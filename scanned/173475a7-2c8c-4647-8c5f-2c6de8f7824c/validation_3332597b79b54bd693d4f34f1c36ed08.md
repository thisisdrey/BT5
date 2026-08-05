## Analog Identified: `pallet-asset-conversion::do_add_liquidity` uses raw, donatable pool-account balances as reserves for its ratio-based liquidity math

### Title
Unbalanced token donation to the deterministic pool account can DoS `add_liquidity` in `pallet-asset-conversion` - (`substrate/frame/asset-conversion/src/lib.rs`)

### Summary
The Sherlock report's root cause is that `SolidlyV3AMO.addLiquidity` derives the LP amount to mint from a *live balance ratio* (`usdAmount * currentLiquidity / usd.balanceOf(pool)`) rather than a manipulation-resistant invariant, and since anyone can permissionlessly add an unbalanced position to the pool, an attacker can skew that ratio and force `addLiquidity` to revert (DoS). `pallet-asset-conversion::do_add_liquidity` has the same structural weakness: it reads `reserve1`/`reserve2` directly from the pool account's *current* asset balances via `Self::get_balance` rather than from a state variable that only changes on `add_liquidity`/`remove_liquidity`/`swap`, so anyone can pre-inflate one side of the reserve by simply transferring tokens to the (deterministic, publicly computable) pool account, corrupting the ratio used by `quote()` for the next legitimate liquidity provider.

### Finding Description
`do_add_liquidity` computes reserves and then the "optimal" amounts using a constant ratio quote: [1](#0-0) 

`reserve1`/`reserve2` come from `Self::get_balance(&pool_account, asset)`, which simply reads the pool account's current on-chain token balance for that asset — not an internally tracked, mint/burn-audited invariant. Because the pool account address is fully deterministic and can be computed by anyone before liquidity even exists (as demonstrated by the existing `cannot_block_pool_creation` test, which pre-funds the pool account with unrelated tokens), an attacker can:

1. Compute the pool account for `asset1`/`asset2` via `PoolLocator::pool_address`.
2. Transfer a large, unbalanced amount of only `asset1` (or `asset2`) directly to that account using an ordinary `Assets::transfer`/`Balances::transfer` call — no `add_liquidity` call, no LP tokens minted, no privileged action needed.
3. This inflates `reserve1` relative to `reserve2` without changing `total_supply` of LP tokens.

When a legitimate user then calls `add_liquidity` with amounts matched to the real market price, `Self::quote` recomputes `amount2_optimal`/`amount1_optimal` against the now-skewed reserves: [2](#0-1) 

This is very likely to breach the caller's `amount1_min`/`amount2_min` slippage checks or fail the `OptimalAmountLessThanDesired` check, causing `add_liquidity` to revert: [3](#0-2) 

Existing tests confirm the pool account is externally computable and pre-fundable before any real liquidity exists, and that this donation channel is only partially mitigated (only for *unrelated* non-sufficient assets used in price *quoting*, not for the actual pool assets used in `add_liquidity`'s reserve/ratio math): [4](#0-3) [5](#0-4) 

The "unrelated assets ignored" fix and the later `prdoc` about switching to full balances for pricing only address *unrelated* asset donations and *reducible-vs-full* balance discrepancies — neither prevents an attacker from donating the *actual pool assets* to skew `reserve1`/`reserve2` used by `do_add_liquidity`'s ratio math: [6](#0-5) 

### Impact Explanation
Any unprivileged account can permanently or repeatedly grief the public `add_liquidity` extrinsic for a given asset pair by donating an unbalanced amount of one pool asset to the deterministic pool account, at essentially no cost (the donated tokens simply remain in the pool, inflating that side's reserve — no burn is required to repeat the effect on future pools). This is a public-entrypoint DoS on a core AMM extrinsic that can be triggered without governance, admin, relayer, validator, or any privileged role — matching the "public underpriced work that degrades... processing" and "runtime bugs that compromise intended behavior" categories in the impact gate. It does not require the attacker to ever call `add_liquidity`/`remove_liquidity` themselves, so ordinary reserve/LP-share safeguards (`MintMinLiquidity`, slippage minimums) do not stop the donation from occurring — they only cause the *legitimate* caller's transaction to revert.

### Likelihood Explanation
High. The pool account address is a pure function of `asset1`/`asset2` and computable off-chain by anyone (`PoolLocator::pool_address`), the donation is a single ordinary `transfer` call with no special permission, and no code path re-normalizes reserves against the LP-token-implied invariant before `quote()` is invoked in `do_add_liquidity`. The existing tests demonstrate the exact pre-funding technique already works for pool-creation griefing; extending the same technique to the actual pool assets to corrupt `add_liquidity`'s ratio math is straightforward.

### Recommendation
Do not derive `reserve1`/`reserve2` used for the add-liquidity ratio calculation purely from the pool account's live external balance. Track reserves as pallet storage that is only updated atomically as part of `do_add_liquidity`/`do_remove_liquidity`/swap execution (Uniswap V2 style `sync`-free invariant), or explicitly `sync` reserves only through code paths that also adjust total LP supply proportionally, so that unsolicited external transfers to the pool account cannot influence the ratio used for minting/optimal-amount calculations.

### Proof of Concept
1. Create a pool for `(asset1, asset2)` via `create_pool`; note the deterministic pool account from `PoolLocator::pool_address(&asset1, &asset2)`.
2. Attacker (no relation to the pool) calls `Assets::transfer` to send a large amount of `asset1` only, directly to the pool account (no `add_liquidity` call, mirroring the pattern in `cannot_block_pool_creation`, but targeting the actual pool asset instead of an unrelated one).
3. A legitimate LP calls `add_liquidity` with `amount1_desired`/`amount2_desired` matching the pre-donation market ratio and reasonable `amount1_min`/`amount2_min`.
4. `Self::quote` in `do_add_liquidity` computes `amount2_optimal`/`amount1_optimal` against the now-skewed `reserve1`, causing `AssetTwoDepositDidNotMeetMinimum`, `AssetOneDepositDidNotMeetMinimum`, or `OptimalAmountLessThanDesired` to fire, reverting the legitimate LP's transaction — reproducing the same "unbalanced third-party deposit breaks the public add-liquidity entrypoint" DoS pattern described in the external report.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-844)
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

**File:** substrate/frame/asset-conversion/src/tests.rs (L974-1020)
```rust
#[test]
fn quote_price_ignores_unrelated_non_sufficient_assets_in_pool_account() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let dot = NativeOrWithId::Native;
		let token = NativeOrWithId::WithId(2);
		let pool_id = (dot.clone(), token.clone());

		create_tokens(user, vec![token.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(dot.clone()),
			Box::new(token.clone())
		));

		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), user, 100000));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 1000));
		assert_ok!(AssetConversion::add_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(dot.clone()),
			Box::new(token.clone()),
			10000,
			200,
			1,
			1,
			user,
		));

		let price = AssetConversion::quote_price_exact_tokens_for_tokens(
			token.clone(),
			dot.clone(),
			60,
			true,
		);
		assert_eq!(price, Some(2302));

		assert_ok!(Assets::force_create(RuntimeOrigin::root(), 3, user, false, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 3, user, 1000));
		let pool_account = <Test as Config>::PoolLocator::address(&pool_id).unwrap();
		assert_ok!(Assets::transfer(RuntimeOrigin::signed(user), 3, pool_account, 1));

		assert_eq!(
			AssetConversion::quote_price_exact_tokens_for_tokens(token, dot, 60, true),
			price
		);
	});
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
