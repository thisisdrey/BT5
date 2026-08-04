### Title
Swap/quote logic accepts un-registered "phantom" pool accounts because `get_reserves` never checks the `Pools` registry - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` is the Uniswap-V2-style AMM in polkadot-sdk. Just like the reported `UniswapV2Library.pairForWithDelegates`, the pallet derives a pool's account **deterministically** from the asset pair via `T::PoolLocator::pool_address` [1](#0-0) , but the core reserve-reading routine `get_reserves` never verifies that this deterministically-derived pool was actually registered through `create_pool` (i.e. it never checks `Pools::<T>::contains_key`). This mirrors exactly the reported bug-class: an address computed as if the pair were properly set up, used without confirming the registry entry exists.

### Finding Description
`Pallet::get_reserves` computes the pool account purely from the asset pair and reads raw balances there: [2](#0-1) 

Note there is no `Pools::<T>::contains_key(&pool_id)` check here — only a check that balances are non-zero (`PoolEmpty`/otherwise `Ok`). Compare this to `do_add_liquidity`/`remove_liquidity`, which correctly gate on the `Pools` storage map, e.g. `remove_liquidity` requires `Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?` [3](#0-2) , and `do_create_pool`/`setup_pool_from_genesis` explicitly `ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists)` before inserting the registry entry [4](#0-3) .

Because `get_reserves` is the sole source of truth used by `quote_price_exact_tokens_for_tokens` (used directly for swap quoting and by the `AssetConversionApi` runtime API and the asset-conversion precompile's `getReserves`) [5](#0-4) , any account that anyone can pre-fund at the well-known deterministic address (exactly as demonstrated by the existing `cannot_block_pool_creation` test, which shows an attacker computing and funding the pool address before `create_pool` is even called) [6](#0-5)  will be treated by `get_reserves`/quoting logic as if it were a legitimate, officially-created pool — even though no `PoolInfo`/LP token exists for it.

This is the direct structural analog of the reported issue: `pairForWithDelegates` derives an address without consulting the `delegates` registry; `pool_address`/`get_reserves` derive an address without consulting the `Pools` registry. The Sweep-n-Flip case fails safe (revert on missing registration because `getReserves` on a non-existent contract reverts); here it fails *unsafe* because Substrate account balances exist unconditionally at any address, so `get_reserves` silently succeeds on unregistered phantom pools instead of reverting.

### Impact Explanation
Downstream consumers (`quote_price_exact_tokens_for_tokens`/`get_reserves` via the `AssetConversionApi` runtime API, the asset-conversion precompile, and `pallet-asset-conversion-tx-payment`'s fee-in-asset quoting logic, which explicitly relies on `AssetConversion::quote_price_exact_tokens_for_tokens` for fee refund computation [7](#0-6) ) treat balances at an unregistered, attacker-fundable account as valid pool reserves. Since no `PoolInfo`/LP token is registered for such a phantom pool, liquidity providers cannot withdraw via `remove_liquidity` (which correctly requires `Pools::contains_key`), meaning any tokens routed through or quoted against a phantom pool are unaccounted for and can be manipulated or drained by whoever interacts with the deterministic address first — directly affecting fee amounts charged/refunded in the transaction-payment extension and any downstream logic trusting `get_reserves`/quote results as authoritative pool state.

### Likelihood Explanation
Likelihood is moderate: the deterministic pool address is derivable off-chain by anyone with `T::PoolLocator`/`PalletId` knowledge (public parameters), requires only an ordinary balance transfer (no privileged action, no malicious validator/relayer/admin), and the `cannot_block_pool_creation` test already proves attackers routinely pre-fund these addresses before official pool creation. No governance or privileged actor is required to trigger the mismatch between "funded address" and "registered pool."

### Recommendation
Have `get_reserves` (and any other reserve-reading path used for quoting/swapping) first check `Pools::<T>::contains_key(&pool_id)` and return `Error::<T>::PoolNotFound` if the pool was never officially created via `create_pool`/genesis setup, mirroring the guard already present in `remove_liquidity`. This prevents phantom, unregistered balances from being treated as authoritative AMM reserves.

### Proof of Concept
1. Attacker computes `pool_account = T::PoolLocator::pool_address(&asset1, &asset2)` off-chain (deterministic, public formula) for an asset pair that has never had `create_pool` called.
2. Attacker transfers a small nonzero balance of `asset1` and `asset2` directly to `pool_account` (ordinary signed transfer, as shown in `cannot_block_pool_creation`) [6](#0-5) .
3. Any caller now invokes `quote_price_exact_tokens_for_tokens(asset1, asset2, amount, ...)` or the `getReserves` precompile/runtime API; `get_reserves` succeeds and returns the attacker-controlled balances as valid reserves [8](#0-7) , despite `Pools::<T>::contains_key(&pool_id)` being `false` and no LP token ever having been issued.

### Citations

**File:** substrate/frame/asset-conversion/src/types.rs (L56-65)
```rust
	/// Retrieves the account address associated with a given asset pair.
	///
	/// Returns an error if the asset pair isn't supported.
	fn pool_address(asset1: &AssetKind, asset2: &AssetKind) -> Result<AccountId, ()> {
		if let Ok(id) = Self::pool_id(asset1, asset2) {
			Self::address(&id)
		} else {
			Err(())
		}
	}
```

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

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L1596-1627)
```rust
			// Derive the pool's account and dust its asset reserve: burn the full balance
			// with `Expendable`, which reaps the asset account. `get_reserves` will then
			// report `asset_reserve == 0` → `get_amount_out` → `Err(ZeroLiquidity)` →
			// `quote_price_exact_tokens_for_tokens` returns `None`.
			let pool_account =
				<<Runtime as pallet_asset_conversion::Config>::PoolLocator as PoolLocator<
					_,
					_,
					_,
				>>::pool_address(&NativeOrWithId::Native, &NativeOrWithId::WithId(asset_id))
				.unwrap();
			let pool_asset_balance = Assets::balance(asset_id, &pool_account);
			assert!(pool_asset_balance > 0);
			assert_ok!(Assets::burn_from(
				asset_id,
				&pool_account,
				pool_asset_balance,
				Preservation::Expendable,
				Precision::Exact,
				Fortitude::Force,
			));
			assert_eq!(Assets::balance(asset_id, &pool_account), 0);

			// Sanity: the refund-direction quote now returns `None` — this is the signal
			// that triggers Path C.
			assert!(AssetConversion::quote_price_exact_tokens_for_tokens(
				NativeOrWithId::Native,
				NativeOrWithId::WithId(asset_id),
				1u64,
				true,
			)
			.is_none());
```
