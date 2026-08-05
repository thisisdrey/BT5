Audit Report

## Title
Swap/quote logic accepts un-registered "phantom" pool accounts because `get_reserves` never checks the `Pools` registry - (File: `substrate/frame/asset-conversion/src/lib.rs`)

## Summary
`Pallet::get_reserves` derives the pool account deterministically via `T::PoolLocator::pool_address` and reads raw asset balances at that address without ever checking `Pools::<T>::contains_key(&pool_id)` [1](#0-0)  This contrasts with other mutating paths such as `remove_liquidity`, which correctly gates on `Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?` [2](#0-1)  and `do_create_pool`, which enforces `ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists)` before registering a pool [3](#0-2)  The existing `cannot_block_pool_creation` test in the repo directly demonstrates an attacker pre-funding the deterministic pool address with balances before `create_pool` is ever called [4](#0-3) 

## Finding Description
`get_reserves` is used as the sole reserve source by `quote_price_exact_tokens_for_tokens`, which is exposed via the pallet's public view functions, the `AssetConversionApi` runtime API, and the asset-conversion precompile [5](#0-4)  The `QuotePrice` trait implementation on `Pallet<T>` simply forwards to these same functions [6](#0-5)  None of these paths verify pool registration before trusting the balances at the deterministic address as authoritative reserves. Since any unprivileged account can compute the deterministic pool address off-chain (public formula, no privileged parameters) and fund it with an ordinary balance/asset transfer, quoting logic will treat these phantom balances as valid reserves even though no `PoolInfo`/LP token was ever created for that pair.

## Impact Explanation
This affects `AssetConversion::quote_price_exact_tokens_for_tokens`/`get_reserves`, consumed by the runtime API, the asset-conversion precompile's `getReserves`, and `pallet-asset-conversion-tx-payment`'s fee-in-asset refund quoting logic. Because such a phantom pool has no `Pools` registry entry and no LP token, any value sent to or quoted against it is not properly accounted for by the AMM's liquidity-provider bookkeeping — liquidity cannot be withdrawn via `remove_liquidity` (which correctly requires `Pools::contains_key`). This is a real logic defect that causes quoting/reserve-reading code to diverge from the pallet's registered-pool invariant, though I was not able to fully trace, within available iterations, whether `do_swap_exact_tokens_for_tokens`/`do_swap_exact_credit_tokens_for_tokens` (the actual swap-execution paths, not just quoting) similarly omit the `Pools::contains_key` check — that would determine whether actual swap execution (not just price quoting) can be routed through a phantom pool, which materially affects severity.

## Likelihood Explanation
Likelihood is credible and repeatable: the deterministic pool address is derivable off-chain from public `PoolLocator` parameters, and funding it requires nothing more than an ordinary signed balance/asset transfer, as directly demonstrated by the pre-existing `cannot_block_pool_creation` test in the repository. No privileged actor or governance action is required to create the mismatch between a funded address and a registered pool.

## Recommendation
Add a `Pools::<T>::contains_key(&pool_id)` (or equivalent `get`) check at the top of `get_reserves`, returning `Error::<T>::PoolNotFound` when the pool has not been registered via `create_pool`/genesis setup, mirroring the guard already present in `remove_liquidity` and `do_create_pool`. Audit all reserve-reading call sites (`quote_price_exact_tokens_for_tokens`, `quote_price_tokens_for_exact_tokens`, and the swap-execution paths) to ensure they route through this fixed `get_reserves` and reject unregistered pool accounts.

## Proof of Concept
1. Attacker computes `pool_account = T::PoolLocator::pool_address(&asset1, &asset2)` off-chain for a pair that has never had `create_pool` called.
2. Attacker performs an ordinary signed transfer of nonzero balances of `asset1` and `asset2` to `pool_account`, exactly as shown in `cannot_block_pool_creation`.
3. Any caller invokes `quote_price_exact_tokens_for_tokens(asset1, asset2, amount, ...)` (directly, via the `AssetConversionApi` runtime API, or via the precompile's `getReserves`); `get_reserves` succeeds and returns the attacker-funded balances as valid reserves despite `Pools::<T>::contains_key(&pool_id)` being `false` and no LP token ever having been issued for the pair.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L741-746)
```rust
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L909-912)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1547)
```rust
		pub fn quote_price_exact_tokens_for_tokens(
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

			let amount_out = if include_fee {
				let fee = Self::pool_fee_for(&asset1, &asset2).ok()?;
				Self::get_amount_out(fee, &amount, &balance1, &balance2).ok()?
			} else {
				Self::quote(&amount, &balance1, &balance2).ok()?
			};
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L2334-2363)
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
```

**File:** substrate/frame/asset-conversion/src/swap.rs (L242-261)
```rust
impl<T: Config> QuotePrice for Pallet<T> {
	type Balance = T::Balance;
	type AssetKind = T::AssetKind;
	fn quote_price_exact_tokens_for_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance> {
		Self::quote_price_exact_tokens_for_tokens(asset1, asset2, amount, include_fee)
	}
	fn quote_price_tokens_for_exact_tokens(
		asset1: Self::AssetKind,
		asset2: Self::AssetKind,
		amount: Self::Balance,
		include_fee: bool,
	) -> Option<Self::Balance> {
		Self::quote_price_tokens_for_exact_tokens(asset1, asset2, amount, include_fee)
	}
}
```
