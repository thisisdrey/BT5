### Title
`get_reserves`/swap-path validation trusts raw pool-account balances without checking pool registration, enabling phantom "pools" that corrupt swap pricing - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
The Vader bug's core defect is that `Pools.swap` never verifies the caller-supplied `base` argument is one of the two legitimate reserve tokens, so an attacker can make the AMM math read an arbitrary/self-controlled balance as if it were a real reserve. The `pallet-asset-conversion` analog is that `Pallet::get_reserves` and `Pallet::validate_swap_path` never verify that a `PoolId` derived from an asset pair actually corresponds to an *officially created* pool (`Pools::<T>::contains_key`). They only require that `T::PoolLocator::pool_address` can deterministically derive an account for the pair, then read whatever raw token balance happens to sit at that address.

### Finding Description
`get_reserves` computes the pool account purely from the asset pair and reads live balances, with no registration check: [1](#0-0) 

`validate_swap_path`, used by every public swap entrypoint, only enforces that each hop's `pool_id` is *unique* within the path — it never checks `Pools::<T>::contains_key(&pool_id)`: [2](#0-1) 

`balance_path_from_amount_in`/`balance_path_from_amount_out` (used by `do_swap_exact_tokens_for_tokens` / `do_swap_tokens_for_exact_tokens`, and by the `Swap`/`SwapCredit` trait impls exposed to the EVM precompile) call `Self::get_reserves(asset1, asset2)` for every hop and feed the result straight into `get_amount_out`/`get_amount_in`: [3](#0-2) 

`T::PoolLocator::address` (e.g. `AccountIdConverter`) is a pure deterministic function `blake2_256(PalletId, pool_id)` — anyone can compute it off-chain without ever calling `create_pool`: [4](#0-3) 

Because that account is an ordinary `AccountId`, any unprivileged user can transfer arbitrary amounts of any two assets into it via normal `transfer` calls, without ever registering the pair in the `Pools` storage map: [5](#0-4) 

The result: a caller can seed a "phantom pool" for any asset pair with an arbitrarily skewed ratio (e.g. tiny `reserve_in`, huge `reserve_out`) purely from tokens they already control (including permissionlessly-created `pallet-assets` assets), then route it as one hop of `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`, or have it picked up by `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens`, which is the exact function the `pallet-asset-conversion-tx-payment` fee-conversion logic and the AssetConversion precompile's `quoteExactTokensForTokens`/`quoteTokensForExactTokens` rely on to price non-native-asset transaction fees and swaps: [6](#0-5) 

This is structurally identical to the Vader flaw: an unchecked identity input (`base` there, `pool_id`/reserve balance here) is trusted by the AMM pricing formula without confirming it is bound to the canonical, LP-backed pool.

### Impact Explanation
An unprivileged attacker can manufacture a fake AMM reserve pair with an arbitrary exchange ratio and inject it as a swap hop or as the source of a price quote, without any liquidity provider ever adding real liquidity or calling `create_pool`. Where this feeds into fee-payment logic (`pallet-asset-conversion-tx-payment`) or public quote endpoints/precompiles that downstream contracts trust for routing/pricing, it allows underpriced fee settlement or manipulated swap accounting — matching the "public underpriced work" / "runtime bug that compromises intended behavior" impact category. It also lets an attacker pre-seed (donate to) the deterministic pool account for a pair before it is ever officially created, corrupting the effective reserve state that `add_liquidity`/`get_reserves` will observe once the pool is later created, since LP shares are minted from caller-declared `amount1_desired`/`amount2_desired`, not from a verified pre-donation-free balance.

### Likelihood Explanation
No privileged role, governance action, validator, or off-chain infrastructure is required — any signed account can transfer tokens to a deterministically-computable address and then call the public `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` extrinsics, or trigger `quote_price_*` via the runtime API/precompile, both of which are ordinary, permissionless public entrypoints.

### Recommendation
`get_reserves` (and therefore `validate_swap_path`/`balance_path_from_amount_in`/`balance_path_from_amount_out`) should reject any asset pair whose `pool_id` is not present in `Pools::<T>`, mirroring the recommended Vader fix of validating that the traded asset is one of the canonical reserve assets, e.g.:
```rust
ensure!(Pools::<T>::contains_key(&pool_id), Error::<T>::PoolNotFound);
```
before reading balances as reserves in `get_reserves`, so swap and quote paths can never treat an unregistered, attacker-seeded account as a legitimate liquidity pool.

### Proof of Concept
1. Attacker computes `pool_address = T::PoolLocator::address(&pool_id)` for an arbitrary pair `(A, B)` — no `create_pool` call needed.
2. Attacker transfers a small amount of `A` and a large amount of `B` (e.g. a permissionlessly-created `pallet-assets` asset they control) directly to `pool_address` via normal `transfer`.
3. Attacker calls `swap_exact_tokens_for_tokens` with `path = [A, B, C]` where `(B, C)` is a real, LP-backed pool. `validate_swap_path` only checks uniqueness of `(A,B)` and `(B,C)` pool ids, not that `(A,B)` is in `Pools::<T>`.
4. `get_reserves(A, B)` returns the attacker-seeded balances as if legitimate, and `get_amount_out` computes an output for hop `(A,B)` skewed entirely by the attacker's chosen ratio, feeding a manipulated `B` amount into the real `(B, C)` pool.
5. The same `get_reserves`/`quote_price_exact_tokens_for_tokens` path, if consumed by `pallet-asset-conversion-tx-payment` for fee conversion between a non-native fee asset and the native token, lets the attacker fabricate a favorable "pool" for the fee-asset pair and pay an artificially low amount of the fee asset for transaction fees.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L213-217)
```rust
	/// Map from `PoolAssetId` to `PoolInfo`. This establishes whether a pool has been officially
	/// created rather than people sending tokens directly to a pool's public account.
	#[pallet::storage]
	pub type Pools<T: Config> =
		StorageMap<_, Blake2_128Concat, T::PoolId, PoolInfo<T::PoolAssetId>, OptionQuery>;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1318-1341)
```rust
		/// Following an amount into a `path`, get the corresponding amounts out.
		pub(crate) fn balance_path_from_amount_in(
			amount_in: T::Balance,
			path: Vec<T::AssetKind>,
		) -> Result<BalancePath<T>, DispatchError> {
			let mut balance_path: BalancePath<T> = Vec::with_capacity(path.len());
			let mut amount_out: T::Balance = amount_in;

			let mut iter = path.into_iter().peekable();
			while let Some(asset1) = iter.next() {
				let asset2 = match iter.peek() {
					Some(a) => a,
					None => {
						balance_path.push((asset1, amount_out));
						break;
					},
				};
				let fee = Self::pool_fee_for(&asset1, asset2)?;
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset1, amount_out));
				amount_out = Self::get_amount_out(fee, &amount_out, &reserve_in, &reserve_out)?;
			}
			Ok(balance_path)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1465-1484)
```rust
		/// Ensure that a path is valid.
		fn validate_swap_path(path: &Vec<T::AssetKind>) -> Result<(), DispatchError> {
			ensure!(path.len() >= 2, Error::<T>::InvalidPath);
			ensure!(path.len() as u32 <= T::MaxSwapPathLength::get(), Error::<T>::InvalidPath);

			// validate all the pools in the path are unique
			let mut pools = BTreeSet::<T::PoolId>::new();
			for assets_pair in path.windows(2) {
				if let [asset1, asset2] = assets_pair {
					let pool_id = T::PoolLocator::pool_id(asset1, asset2)
						.map_err(|_| Error::<T>::InvalidAssetPair)?;

					let new_element = pools.insert(pool_id);
					if !new_element {
						return Err(Error::<T>::NonUniquePath.into());
					}
				}
			}
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1499-1513)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1516-1547)
```rust
		/// Gets a quote for swapping an exact amount of `asset1` for `asset2`.
		///
		/// If `include_fee` is true, the quote will include the liquidity provider fee.
		/// If the pool does not exist or has no liquidity, `None` is returned.
		/// Note that the price may have changed by the time the transaction is executed.
		/// (Use `amount_out_min` to control slippage.)
		/// Returns `Some(quoted_amount)` on success.
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

**File:** substrate/frame/asset-conversion/src/types.rs (L146-158)
```rust
/// `PoolId` to `AccountId` conversion.
pub struct AccountIdConverter<Seed, PoolId>(PhantomData<(Seed, PoolId)>);
impl<Seed, PoolId, AccountId> TryConvert<&PoolId, AccountId> for AccountIdConverter<Seed, PoolId>
where
	PoolId: Encode,
	AccountId: Decode,
	Seed: Get<PalletId>,
{
	fn try_convert(id: &PoolId) -> Result<AccountId, &PoolId> {
		sp_io::hashing::blake2_256(&Encode::encode(&(Seed::get(), id))[..])
			.using_encoded(|e| Decode::decode(&mut TrailingZeroInput::new(e)).map_err(|_| id))
	}
}
```
