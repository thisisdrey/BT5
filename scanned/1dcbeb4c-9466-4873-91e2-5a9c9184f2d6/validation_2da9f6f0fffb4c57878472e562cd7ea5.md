This confirms the exact analog: `pallet-asset-conversion` exposes both stateful swap entrypoints and a `quoteExactTokensForTokens` view function through a Solidity precompile consumable by `pallet-revive` contracts, and the swap's internal execution order pays out before crediting input.

### Title
Read-only reentrancy in `pallet-asset-conversion` swap path via `pallet-revive` precompile view-quote during mid-swap state - (File: `substrate/frame/asset-conversion/src/lib.rs`, `substrate/frame/asset-conversion/precompiles/src/lib.rs`)

### Summary
`Pallet::credit_swap` (and the `swap` wrapper it powers) executes a multi-hop trade by (1) transferring the intermediate/output asset out of the source pool(s) first, and only as the *last* step (2) crediting the input asset into the first pool. Between steps (1) and (2), pool reserves are asymmetric: the output side has already been drained but the input side has not yet been topped up. `pallet-revive` exposes `quote_price_exact_tokens_for_tokens`/`get_reserves` as a public, callable "view" via the `AssetConversion` precompile (`quoteExactTokensForTokens`), reachable synchronously from any Solidity contract during its execution. If a contract-backed asset is used as one leg of a pool and its `transfer`/`resolve` callback re-enters the runtime (e.g., an asset whose issuance/transfer is mediated by a `pallet-revive` contract acting as an ERC-20-with-hooks style token), that callback can call the precompile's quote function while the swap is mid-flight, observing the drained-but-not-yet-credited reserve state and obtaining a manipulated price — the same "read stale/inconsistent AMM state via a view call during execution" primitive as the Curve `get_virtual_price` read-only reentrancy bug.

### Finding Description
`Pallet::credit_swap` in `substrate/frame/asset-conversion/src/lib.rs` resolves a swap path hop-by-hop: [1](#0-0) 
For intermediate hops it moves `asset2` directly from `pool_from` to `pool_to`; for the final hop it withdraws `asset2` from the last pool into `credit_out` and returns immediately — **before** the original input credit is ever placed into the first pool. The crediting of `credit_in` into `pool_to` (the first pool) happens only afterward: [2](#0-1) 

This means that at the point `credit_out` has been produced (and, in `Pallet::swap`, resolved to `send_to`), the first pool's reserves reflect the *pre-swap* input balance and the *post-swap* output balance simultaneously — an inconsistent, manipulable intermediate state, exactly analogous to Curve's base-pool state during `remove_liquidity`/`add_liquidity` before `get_virtual_price` is safe to read.

`pallet-asset-conversion` is also exposed to `pallet-revive` Solidity contracts through a dedicated precompile that offers both the swap entrypoints and a live price-quote view function: [3](#0-2) 
`quoteExactTokensForTokens` calls straight into `Pallet::quote_price_exact_tokens_for_tokens`, which itself calls `get_reserves` reading live account balances with no lock or guard against being read mid-transaction: [4](#0-3) [5](#0-4) 

`swapExactTokensForTokens` in the precompile invokes `Swap::swap_exact_tokens_for_tokens`, which is the entrypoint into the above inconsistent-state sequence: [6](#0-5) 

Unlike Curve's newer base pools, `pallet-asset-conversion`'s `get_reserves`/`quote_price_exact_tokens_for_tokens` have no reentrancy guard/lock analogous to a `nonreentrant` modifier — there is no storage flag preventing a nested call into these view functions while a swap for the same pool is mid-execution. Any asset kind whose transfer/resolve/withdraw hooks can trigger execution of external code before dispatch of the enclosing extrinsic completes (e.g., an asset backed by a `pallet-revive` contract with transfer hooks, or any future `T::Assets` implementation that runs contract code as part of `transfer`/`resolve`) creates the callback opportunity needed to observe and act on the intermediate state.

### Impact Explanation
If a contract can re-enter and read a manipulated quote mid-swap (analogous to reading a manipulated `get_virtual_price`), it can be paid out or make downstream decisions (e.g., triggering another swap or liquidation logic that consumes the quote) using stale/asymmetric reserve data, leading to funds loss for the pool or for a third-party protocol trusting the quote — the same "critical, fund-loss via price manipulation" class as the original report.

### Likelihood Explanation
Likelihood depends entirely on whether an `AssetKind` usable in `pallet-asset-conversion` pools can be backed by contract code capable of running during `T::Assets::transfer`/`resolve`/`withdraw` (this repo's default `pallet-assets`/`pallet-balances` implementations do not appear to execute arbitrary contract code on transfer, based on the `frame/assets` search performed, which found no generic `on_transfer` contract-callback hook). The precompile explicitly targets Asset Hub / `pallet-revive` deployments pairing DEX assets with contract-issued tokens, which is the scenario where this path becomes exploitable. I could not fully verify, within the scope of this scan, whether any currently-configured `AssetKind`/`T::Assets` implementation in this repository actually permits such a callback during transfer; that would need to be confirmed by inspecting the specific runtime configuration (e.g., Asset Hub runtime's `Fungibles`/`AssetKind` type) that pairs `pallet-asset-conversion` with `pallet-revive`.

### Recommendation
Add a reentrancy guard (a per-pool or global "swap in progress" flag) around `credit_swap`/`swap` in `substrate/frame/asset-conversion/src/lib.rs` that is checked and set before any transfer/withdraw begins and cleared only after `credit_in` has been fully resolved into the first pool, and have `get_reserves`/`quote_price_exact_tokens_for_tokens` (and their precompile-exposed counterparts) reject or flag reads while the guard is set for the queried pool. Alternatively, restructure `credit_swap` to credit `credit_in` into the first pool before paying out any downstream hop, eliminating the drained-but-uncredited intermediate state entirely.

### Proof of Concept
Conceptual sequence (cannot be executed without confirming a contract-backed `AssetKind` capable of mid-transfer callbacks in a concrete runtime):
1. Attacker deploys a `pallet-revive` contract implementing an asset with a transfer hook, and it is paired in a `pallet-asset-conversion` pool as `asset2`.
2. Attacker's contract calls `swapExactTokensForTokens(path=[assetX, asset2], ...)` via the precompile, entering `Pallet::credit_swap`.
3. During the internal `T::Assets::withdraw`/`transfer` of `asset2` out of the pool (before `credit_in` is resolved into the pool per [7](#0-6) ), the attacker's transfer hook re-enters and calls the precompile's `quoteExactTokensForTokens`/`quote_price_exact_tokens_for_tokens`, observing the pool with `asset2` already reduced but `assetX` not yet increased.
4. The attacker uses this skewed quote in a second contract action (e.g., triggering a swap in a different pool or a downstream protocol that trusts this quote as an oracle) to extract value based on the temporarily incorrect price.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1198-1223)
```rust
			let resolve_path = || -> Result<CreditOf<T>, DispatchError> {
				for pos in 0..=path.len() {
					if let Some([(asset1, _), (asset2, amount_out)]) = path.get(pos..=pos + 1) {
						let pool_from = T::PoolLocator::pool_address(asset1, asset2)
							.map_err(|_| Error::<T>::InvalidAssetPair)?;

						if let Some((asset3, _)) = path.get(pos + 2) {
							let pool_to = T::PoolLocator::pool_address(asset2, asset3)
								.map_err(|_| Error::<T>::InvalidAssetPair)?;

							T::Assets::transfer(
								asset2.clone(),
								&pool_from,
								&pool_to,
								*amount_out,
								Preserve,
							)?;
						} else {
							let credit_out =
								Self::withdraw(asset2.clone(), &pool_from, *amount_out, true)?;
							return Ok(credit_out);
						}
					}
				}
				Err(Error::<T>::InvalidPath.into())
			};
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1225-1243)
```rust
			let credit_out = match resolve_path() {
				Ok(c) => c,
				Err(e) => return Err((credit_in, e)),
			};

			let pool_to = if let Some([(asset1, _), (asset2, _)]) = path.get(0..2) {
				match T::PoolLocator::pool_address(asset1, asset2) {
					Ok(address) => address,
					Err(_) => return Err((credit_in, Error::<T>::InvalidAssetPair.into())),
				}
			} else {
				return Err((credit_in, Error::<T>::InvalidPath.into()));
			};

			T::Assets::resolve(&pool_to, credit_in)
				.map_err(|c| (c, Error::<T>::BelowMinimum.into()))?;

			Ok(credit_out)
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

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L50-98)
```rust
alloy::sol! {
	/// Precompile interface for asset-conversion (DEX) operations.
	///
	/// Assets are identified by their SCALE-encoded AssetKind (e.g. xcm::v5::Location)
	/// passed as `bytes`. Contracts can hardcode these as constants or obtain them
	/// off-chain.
	interface IAssetConversion {
		/// Swap an exact amount of input tokens for as many output tokens as possible.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountIn Exact amount of the first asset to swap.
		/// @param amountOutMin Minimum acceptable amount of the last asset to receive.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountOut The amount of output tokens received.
		function swapExactTokensForTokens(
			bytes[] calldata path,
			uint256 amountIn,
			uint256 amountOutMin,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountOut);

		/// Swap tokens to receive an exact amount of output tokens.
		/// @param path Ordered list of SCALE-encoded asset identifiers defining the swap route.
		/// @param amountOut Exact amount of the last asset to receive.
		/// @param amountInMax Maximum acceptable amount of the first asset to spend.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountIn The amount of input tokens spent.
		function swapTokensForExactTokens(
			bytes[] calldata path,
			uint256 amountOut,
			uint256 amountInMax,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountIn);

		/// Quote the expected output for a given exact input swap.
		/// @param asset1 SCALE-encoded identifier of the input asset.
		/// @param asset2 SCALE-encoded identifier of the output asset.
		/// @param amount The input amount to quote for.
		/// @param includeFee Whether to include the pool's LP fee in the quote.
		/// @return The expected output amount.
		function quoteExactTokensForTokens(
			bytes calldata asset1,
			bytes calldata asset2,
			uint256 amount,
			bool includeFee
		) external view returns (uint256);
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L289-319)
```rust
	fn swap_exact_tokens_for_tokens(
		call: &IAssetConversion::swapExactTokensForTokensCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		let path_len = Self::validated_path_len(&call.path)?;
		env.charge(
			<Runtime as pallet_asset_conversion::Config>::WeightInfo::swap_exact_tokens_for_tokens(
				path_len,
			),
		)?;
		let path: Vec<_> =
			call.path.iter().map(|e| Self::decode_asset_kind(e)).collect::<Result<_, _>>()?;

		let sender = Self::caller_account_id(env)?;
		let send_to = env.to_account_id(&H160(call.sendTo.0 .0));

		let amount_out = <pallet_asset_conversion::Pallet<Runtime> as Swap<
			<Runtime as frame_system::Config>::AccountId,
		>>::swap_exact_tokens_for_tokens(
			sender,
			path,
			Self::to_balance(call.amountIn)?,
			Some(Self::to_balance(call.amountOutMin)?),
			send_to,
			call.keepAlive,
		)?;

		Ok(IAssetConversion::swapExactTokensForTokensCall::abi_encode_returns(&Self::to_u256(
			amount_out,
		)?))
	}
```
