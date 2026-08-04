### Title
Direct-transfer donation inflates AMM pool reserves and dilutes LP minting/redemption in `pallet-asset-conversion` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves for liquidity minting and burning by reading the *raw* asset balance of the pool account (`get_balance` → `T::Assets::balance`), rather than an internally accounted reserve value that only changes through `do_add_liquidity`/`do_remove_liquidity`/swap bookkeeping. Because the pool account is a plain, publicly known deterministic account (`T::PoolLocator::address`), anyone can transfer tokens to it directly, outside of `add_liquidity`, and instantly change the reserve figures used for LP-token minting and redemption math. This is the same "donation to inflate share price before minting" primitive as the reported ERC4626 inflation attack on `Water.sol`.

### Finding Description
`get_reserves`/`get_balance` read the pool account's balance directly: [1](#0-0) [2](#0-1) 

`do_add_liquidity` uses this raw balance as `reserve1`/`reserve2` to compute the LP tokens minted to a depositor, proportionally to `total_supply` of the LP token: [3](#0-2) [4](#0-3) 

`do_remove_liquidity` symmetrically redeems LP tokens against the same raw-balance reserves: [5](#0-4) 

Since `reserve1`/`reserve2` are literally `T::Assets::balance(asset, pool_account)`, any unprivileged account can call the underlying `Assets`/`Balances` transfer/mint extrinsic to send tokens straight to the deterministic pool account, changing `reserve1`/`reserve2` without going through `add_liquidity` and without changing `total_supply` of the LP token. That is functionally identical to the ERC4626 "donate to the vault, then let victim's deposit round down" trick: the vault/pool's tracked "assets per share" is derived from a balance that can be moved by anyone, not from state exclusively mutated by the deposit/mint code path.

### Impact Explanation
If an attacker holds (or acquires) some LP tokens for a pool and then donates extra reserve1/reserve2 tokens directly to the pool account, any subsequent depositor's `side1 = mul_div(amount1, total_supply, reserve1)` and `side2` computation is diluted, because the inflated reserve lowers the LP tokens minted per unit contributed (`lp_token_amount = side1.min(side2)`), while the depositor's real capital is transferred in full to the pool account. The attacker can then call `remove_liquidity` to redeem their existing LP tokens against the now-larger reserve pool (which includes the victim's freshly added funds), extracting value that rightfully belongs to the new depositor. This directly violates the "value must conserve and settle exactly once to the rightful beneficiary" invariant for asset-holding pallets.

### Likelihood Explanation
The attack requires no privileged role, no governance, no relayer, and no malicious validator/collator — only: (1) knowledge of the deterministic pool account address (computed via the public `PoolLocator`), (2) enough of the underlying asset to perform a plain transfer to that account, and (3) pre-existing LP token holdings (which the attacker can acquire cheaply as the pool's first liquidity provider). All of this is achievable by any ordinary user through public, unprivileged extrinsics (`transfer`, `add_liquidity`, `remove_liquidity`), matching the required "unprivileged attacker, public entry point" profile.

### Recommendation
Track pool reserves as pallet storage state that is mutated only inside `do_add_liquidity`, `do_remove_liquidity`, and swap execution, instead of deriving them live from `T::Assets::balance`. If raw balances must be used for compatibility, the pallet should reconcile/ignore un-recorded balance deltas (e.g., only credit reserves that were deposited via `add_liquidity`) so that arbitrary donations cannot change the price used for LP minting/burning — the same mitigation OpenZeppelin recommends for ERC4626 (decimals offset / virtual shares, or accounting balances rather than raw token balances).

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` and `add_liquidity` with a small amount, becoming an LP holder of `pool_id`.
2. Attacker transfers a large amount of `asset1` directly to the pool's deterministic account (`PoolLocator::address(&pool_id)`), bypassing `add_liquidity` entirely — this is a normal `Assets::transfer`/`Balances::transfer`, requiring no special permission.
3. `get_reserves`/`get_balance` (`substrate/frame/asset-conversion/src/lib.rs:1499-1514`) now report an inflated `reserve1` while `total_supply` of the LP token is unchanged.
4. A victim calls `add_liquidity` with real capital; `do_add_liquidity`'s `side1 = mul_div(amount1, total_supply, reserve1)` (`lib.rs:869-871`) mints a diluted amount of LP tokens relative to the capital contributed.
5. Attacker calls `remove_liquidity`, redeeming their original LP tokens against `do_remove_liquidity`'s `amount1 = mul_div(lp_redeem_amount, reserve1, total_supply)` (`lib.rs:919-920`), now proportionally larger because `reserve1` includes both the donation and the victim's newly added funds — extracting value that should belong to the victim. [6](#0-5) 
This existing test (`cannot_block_pool_creation`) confirms the pool account's raw balance is indeed attacker-influenceable via direct token transfers before/around liquidity operations, though that test only checks pool-creation is not blocked — it does not check for LP-token dilution/extraction, which remains unmitigated.

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L911-920)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
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
