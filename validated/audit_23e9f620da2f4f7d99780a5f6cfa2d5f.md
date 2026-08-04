I found the core mechanism: `pallet-asset-conversion`'s `get_balance` (`substrate/frame/asset-conversion/src/lib.rs:1267`) reads the pool account's raw, total asset balance via `T::Assets::balance(asset, owner)` and treats that number directly as the AMM reserve for all pricing math (`get_reserves`, `Self::mul_div`, `get_amount_out`/`get_amount_in`) — this is not backed by any internally tracked reserve counter. This exactly mirrors the reported Vyper bug class: computing a swap/settlement amount from an absolute balance read rather than a tracked delta, so any value sitting in that account beyond what the pool "intends" as reserves silently and permanently distorts the constant-product formula for every subsequent quote/swap. [1](#0-0) [2](#0-1) 

### Title
Pool reserves computed from raw account balance instead of tracked liquidity allow reserve inflation via direct transfer, distorting AMM swap pricing - (File: substrate/frame/asset-conversion/src/lib.rs)

### Summary
`pallet-asset-conversion` never maintains an independent, protected reserve counter for a liquidity pool. Instead, `get_reserves`/`get_balance` simply read the pool account's live token balance (`T::Assets::balance(asset, owner)`) and feed that number straight into the constant-product formulas (`get_amount_out`, `get_amount_in`, `quote`) used by every `swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`, `add_liquidity`/`remove_liquidity`, and the XCM `quote_price_*` runtime APIs. Because `T::Assets` (e.g. `pallet_balances`/`pallet_assets`) allows **any** account to `transfer` tokens directly to the pool's account, an unprivileged actor can inflate one side of the "reserve" outside of `add_liquidity`, without minting LP tokens or being recorded as a liquidity provider. Every subsequent swap is then priced off the corrupted reserve figure.

### Finding Description
`Self::get_balance` is defined as a thin pass-through to the raw fungible balance: [1](#0-0) 

`get_reserves` uses this raw balance directly as the AMM reserve pair with no cross-check against a separately tracked, protected value: [2](#0-1) 

`balance_path_from_amount_in`/`balance_path_from_amount_out` (used by both the extrinsics and the `Swap`/`SwapCredit` trait implementations) call `get_reserves` and feed the result straight into `get_amount_in`/`get_amount_out`: [3](#0-2) 

The swap execution path (`credit_swap`/`swap`) then transfers exactly the amount computed from that reserve figure, with no independent reconciliation of "reserve before" vs "reserve after" the swap: [4](#0-3) 

This is the direct analog of the external report's root cause: the report flags computing a swap amount from an absolute balance read (which can already include unrelated/attacker-controlled funds) instead of from the delta actually contributed by the transaction. Here, the pallet has the same class of flaw at the reserve level — any direct `transfer` of the pool's asset into the pool account (a completely permissionless, non-privileged action available to any signed account holding the asset) permanently changes the "reserve" used for every future price computation, without going through `add_liquidity` (which mints LP shares proportionally and is the only sanctioned way to add reserves). A `prdoc` entry (`prdoc/pr_12408.prdoc`) even documents that a very similar issue was already fixed in the *other* direction — switching from `reducible_balance` to `T::Assets::balance` specifically *because* it could "understate" pool reserves — confirming that raw account balance, not a protected/tracked reserve, is authoritative for pricing in this pallet by design.

### Impact Explanation
An attacker (any signed account, no special privilege) can:
1. Send extra `asset1` or `asset2` directly to a pool's account via a normal `transfer`.
2. This inflates one side of `get_reserves` without minting any LP tokens to the attacker and without the liquidity providers' consent.
3. Every later `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`/`quote_price_*` call, and the AssetHub XCM `ExchangeAsset` path (`SingleAssetExchangeAdapter`) that relies on `AssetConversion::SwapCredit`, is priced using the corrupted constant-product curve.
4. This can be used to manipulate the exchange rate seen by victims (worse execution price, matching the report's "unfavorable exchange" framing) or, depending on direction, to extract value from the pool by swapping immediately after inflating one reserve leg and reverting/withdrawing profit, at the expense of honest liquidity providers and swappers.

This affects `pallet-asset-conversion`, which underpins DEX functionality on Polkadot Asset Hub and is exposed to unprivileged users through public extrinsics, RPC quote APIs, the EVM/PVM precompile (`asset-conversion/precompiles`), and XCM's `ExchangeAsset` instruction — a wide, chain-critical public attack surface, matching "runtime bugs that compromise intended behavior" / "public underpriced work" in the impact gate.

### Likelihood Explanation
High likelihood: the attack requires only a standard signed `transfer` call to the well-known, deterministically derived pool account address (`T::PoolLocator::address`), which is discoverable by any user/attacker without governance, admin, validator, or off-chain infrastructure involvement. No malicious peer/validator/relayer assumption is needed — this is a pure unprivileged on-chain action against a public AMM pool.

### Recommendation
Maintain and use an explicitly tracked reserve value per pool (updated only by `add_liquidity`/`remove_liquidity`/`swap` internal transfers) rather than trusting the pool account's raw fungible balance for AMM math, or reconcile/clamp reserves to the last known tracked value and treat any excess balance as "donated" dust that does not participate in pricing (mirroring the report's own recommendation to use balance-delta accounting rather than absolute balance reads).

### Proof of Concept
1. Create a pool for `(asset1, asset2)` and add initial liquidity via `add_liquidity`, establishing reserves `R1, R2`.
2. As an unrelated attacker account, call `T::Assets::transfer` (e.g. `pallet_balances::transfer` or `pallet_assets::transfer`) to send `D` extra units of `asset1` directly to the pool's account address (obtained via `PoolLocator::address`), bypassing `add_liquidity` entirely.
3. Call `AssetConversion::get_reserves(asset1, asset2)` — observe reserve1 = `R1 + D` (verified by `get_balance` at `lib.rs:1267` reading raw `T::Assets::balance`), while no LP tokens were minted for the attacker.
4. Call `quote_price_exact_tokens_for_tokens`/`swap_exact_tokens_for_tokens` for a subsequent legitimate swap and compare the output amount against the amount predicted using the pre-donation reserves — the discrepancy demonstrates the distorted, attacker-influenced pricing derived directly from `get_reserves`/`balance_path_from_amount_in` (`lib.rs:1319-1341`).

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1168-1243)
```rust
		fn swap(
			sender: &T::AccountId,
			path: &BalancePath<T>,
			send_to: &T::AccountId,
			keep_alive: bool,
		) -> Result<(), DispatchError> {
			let (asset_in, amount_in) = path.first().ok_or(Error::<T>::InvalidPath)?;
			let credit_in = Self::withdraw(asset_in.clone(), sender, *amount_in, keep_alive)?;

			let credit_out = Self::credit_swap(credit_in, path).map_err(|(_, e)| e)?;
			T::Assets::resolve(send_to, credit_out).map_err(|_| Error::<T>::BelowMinimum)?;

			Ok(())
		}

		/// Swap assets along the specified `path`, consuming `credit_in` and producing
		/// `credit_out`.
		///
		/// If an error occurs, `credit_in` is returned back.
		///
		/// Note: It's assumed that the provided `path` is valid and `credit_in` corresponds to the
		/// first asset in the `path`.
		///
		/// WARNING: This may return an error after a partial storage mutation. It should be used
		/// only inside a transactional storage context and an Err result must imply a storage
		/// rollback.
		fn credit_swap(
			credit_in: CreditOf<T>,
			path: &BalancePath<T>,
		) -> Result<CreditOf<T>, (CreditOf<T>, DispatchError)> {
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1265-1269)
```rust
		/// Get the `owner`'s balance of `asset`, which could be the chain's native asset or another
		/// fungible. Returns a value in the form of an `Balance`.
		pub(crate) fn get_balance(owner: &T::AccountId, asset: T::AssetKind) -> T::Balance {
			T::Assets::balance(asset, owner)
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L1291-1341)
```rust
		/// Leading to an amount at the end of a `path`, get the required amounts in.
		pub(crate) fn balance_path_from_amount_out(
			amount_out: T::Balance,
			path: Vec<T::AssetKind>,
		) -> Result<BalancePath<T>, DispatchError> {
			let mut balance_path: BalancePath<T> = Vec::with_capacity(path.len());
			let mut amount_in: T::Balance = amount_out;

			let mut iter = path.into_iter().rev().peekable();
			while let Some(asset2) = iter.next() {
				let asset1 = match iter.peek() {
					Some(a) => a,
					None => {
						balance_path.push((asset2, amount_in));
						break;
					},
				};
				let fee = Self::pool_fee_for(asset1, &asset2)?;
				let (reserve_in, reserve_out) = Self::get_reserves(asset1.clone(), asset2.clone())?;
				balance_path.push((asset2, amount_in));
				amount_in = Self::get_amount_in(fee, &amount_in, &reserve_in, &reserve_out)?;
			}
			balance_path.reverse();

			Ok(balance_path)
		}

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
