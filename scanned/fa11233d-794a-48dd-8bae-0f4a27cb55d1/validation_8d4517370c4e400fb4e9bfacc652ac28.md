### Title
Stale reserve snapshot combined with post-reentrancy `total_supply` in `AssetConversion::do_add_liquidity` allows LP-token over-minting when a pool asset is backed by an ERC20 contract on `pallet-revive` - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet_asset_conversion::Pallet::do_add_liquidity` snapshots the pool reserves (`reserve1`, `reserve2`) *before* calling `T::Assets::transfer`, then reads `total_supply` of the LP token *after* that transfer, and finally computes the amount of LP tokens to mint from the stale reserves and the fresh `total_supply`. When a pool asset is an ERC20 contract whose fungible interface is bridged through `pallet-revive`'s `impl_fungibles.rs` (where every `Inspect`/`Mutate` operation is itself a `bare_call` into arbitrary attacker-controlled Solidity bytecode), the transfer call is a genuine reentrancy point. This exactly mirrors the reported ERC4626 bug class: a "before" balance/ratio is fixed, an external call that can execute attacker code intervenes, and a "delta"/ratio is finalized afterward using mismatched pre/post state.

### Finding Description
In `do_add_liquidity`:
```
let reserve1 = Self::get_balance(&pool_account, asset1.clone());
let reserve2 = Self::get_balance(&pool_account, asset2.clone());
... // amount1, amount2 computed from reserve1/reserve2
T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;
let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
...
let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
lp_token_amount = side1.min(side2);
``` [1](#0-0) 

`reserve1`/`reserve2` are read before the transfer, but `total_supply` is read after it — the two are supposed to represent the same instant, but nothing enforces that if the transfer itself can execute arbitrary code.

`T::AssetKind`/`T::Assets` in this pallet is generic over any `fungibles::Mutate` implementation. `pallet-revive` provides exactly such an implementation for ERC20 contracts deployed on it: `AssetId = H160` (the contract's own address), and every operation (`balance`, `total_issuance`, `mint_into`, `burn_from`) is executed via `Self::bare_call(...)` into that contract's Solidity code:
```
impl<T: Config> fungibles::Inspect<...> for Pallet<T> {
    type AssetId = H160;
    fn total_issuance(asset_id: Self::AssetId) -> Self::Balance {
        let data = IERC20::totalSupplyCall {}.abi_encode();
        let ContractResult { result, .. } = Self::bare_call(..., asset_id, ..., data, ...);
        ...
    }
    fn balance(asset_id: Self::AssetId, account_id: &T::AccountId) -> Self::Balance {
        ... Self::bare_call(..., asset_id, ..., data, ...)
    }
}
``` [2](#0-1) 

and `burn_from`/`mint_into` likewise dispatch an `IERC20::transferCall` into the contract: [3](#0-2) 

Since `T::Assets::transfer` (invoked from `do_add_liquidity`) for such an asset ultimately triggers a `bare_call` into attacker-owned contract code, the attacker's `transfer` implementation can synchronously re-enter the asset-conversion precompile (`substrate/frame/asset-conversion/precompiles/src/lib.rs`, `add_liquidity`) — confirmed reachable, since the precompile itself dispatches straight into `pallet_asset_conversion::Pallet::add_liquidity`: [4](#0-3) 

During that reentrant call the attacker can mint additional LP tokens (inflating `total_supply`) using the pool's *current* real reserves (which already include the just-transferred amount1 that the outer call has not yet accounted for as "reserve"). When control returns to the outer `do_add_liquidity`, `total_supply` is read fresh (now inflated by the reentrant mint) while `reserve1`/`reserve2` remain the stale pre-transfer values, so `side1`/`side2` — and therefore `lp_token_amount` minted to the outer caller — are computed from a mismatched numerator/denominator pair. This directly parallels the reported bug's "before balance" snapshot combined with a post-reentrancy "after balance" used to compute a minted delta.

The pallet's own comment on `do_swap_exact_tokens_for_tokens` — "WARNING: This may return an error after a partial storage mutation. It should be used only inside a transactional storage context and an Err result must imply a storage rollback" — shows an awareness of non-atomicity risk, but this only protects the *error* path via `#[transactional]`, not a *successful* nested reentrant call.

### Impact Explanation
An attacker who registers or controls an ERC20 contract used as a pool asset can mint LP tokens disproportionate to the assets actually contributed, diluting the pool and other liquidity providers' claims, and can redeem those inflated LP tokens for real reserve assets (native/other pool assets) via `remove_liquidity`. This is a direct theft-of-funds / broken value-conservation issue in a public, permissionless entry point (`add_liquidity` / the `IAssetConversion` precompile), matching the "Balances, assets ... must conserve value and settle exactly once" pivot.

### Likelihood Explanation
Any user can create a pool with `create_pool` and supply their own ERC20 contract as one side of the pair (this is exactly what `pallet-revive`'s fungibles ERC20 bridge is designed to enable), then call `add_liquidity` as an unprivileged, permissionless action — no admin, governance, relayer, or validator involvement is required. The reentrancy point is a synchronous nested `bare_call`, not a race condition, so it is deterministically triggerable by the attacker's own contract code.

### Recommendation
Re-read `total_supply` and reserves atomically relative to the transfers, or read `total_supply` (and reserves used for the ratio) *before* performing any asset transfer that could execute foreign code, mirroring the correct fix pattern from the source report ("capture accounting state after/around the reentrant call consistently, not split across it"). Alternatively, disallow/guard `T::Assets` implementations that can execute arbitrary code during `transfer`/`balance`/`total_issuance` from being combined with reserve math that spans a transfer boundary, or add pool-level reentrancy guards around `do_add_liquidity`/`do_remove_liquidity`/`do_swap_*`.

### Proof of Concept
1. Deploy a malicious ERC20 contract `M` on `pallet-revive` whose `transfer(to, amount)` function, when called by the asset-conversion pool account as `from`/effective mover, makes a nested EVM `CALL` to the `IAssetConversion` precompile address invoking `addLiquidity(asset1=M, asset2=DOT, ...)` a second time using the pool's already-updated real balance of `M` (which includes the amount just received in the outer transfer, not yet reflected in the outer call's `reserve1`).
2. Attacker (controlling `M`) calls `AssetConversion::add_liquidity(asset1=M, asset2=DOT, amount1_desired, amount2_desired, ...)`.
3. Pallet reads `reserve1`/`reserve2` (pre-attack state), computes `amount1`/`amount2`, then calls `T::Assets::transfer(M, attacker, pool_account, amount1)` which triggers `bare_call` into `M`'s Solidity `transfer`.
4. `M`'s `transfer` reenters the precompile, calling `add_liquidity` again with the *already-transferred* `M` balance sitting in `pool_account`, correctly minting LP tokens for that inner call at the fair current ratio, inflating `PoolAssets::total_issuance(lp_token)`.
5. Control returns to the outer call; it reads the now-inflated `total_supply`, but still uses the stale `reserve1`/`reserve2` from step 3 to compute `side1`/`side2`, minting `lp_token_amount` to the attacker's `mint_to` that is larger than what the actually-contributed `amount1`/`amount2` justify relative to the pool's real, post-reentrancy reserves.
6. Attacker calls `remove_liquidity` to redeem the excess LP tokens for real DOT/other-asset reserves, extracting value from other liquidity providers.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-872)
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

			ensure!(
				amount1.saturating_add(reserve1) >= T::Assets::minimum_balance(asset1.clone()),
				Error::<T>::AmountOneLessThanMinimal
			);
			ensure!(
				amount2.saturating_add(reserve2) >= T::Assets::minimum_balance(asset2.clone()),
				Error::<T>::AmountTwoLessThanMinimal
			);

			T::Assets::transfer(asset1, who, &pool_account, amount1, Preserve)?;
			T::Assets::transfer(asset2, who, &pool_account, amount2, Preserve)?;

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

**File:** substrate/frame/revive/src/impl_fungibles.rs (L59-123)
```rust
impl<T: Config> fungibles::Inspect<<T as frame_system::Config>::AccountId> for Pallet<T> {
	// The asset id of an ERC20 is its origin contract's address.
	type AssetId = H160;
	// The balance is always u128.
	type Balance = u128;

	// Need to call a view function here.
	fn total_issuance(asset_id: Self::AssetId) -> Self::Balance {
		let data = IERC20::totalSupplyCall {}.abi_encode();
		let ContractResult { result, .. } = Self::bare_call(
			OriginFor::<T>::signed(Self::checking_account()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		if let Ok(return_value) = result &&
			let Ok(eu256) = EU256::abi_decode_validate(&return_value.data)
		{
			eu256.to::<u128>()
		} else {
			0
		}
	}

	fn minimum_balance(_: Self::AssetId) -> Self::Balance {
		// ERC20s don't have this concept.
		1
	}

	fn total_balance(asset_id: Self::AssetId, account_id: &T::AccountId) -> Self::Balance {
		// Since ERC20s don't have the concept of freezes and locks,
		// total balance is the same as balance.
		Self::balance(asset_id, account_id)
	}

	fn balance(asset_id: Self::AssetId, account_id: &T::AccountId) -> Self::Balance {
		let eth_address = T::AddressMapper::to_address(account_id);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		let data = IERC20::balanceOfCall { account: address }.abi_encode();
		let ContractResult { result, .. } = Self::bare_call(
			OriginFor::<T>::signed(account_id.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		if let Ok(return_value) = result &&
			let Ok(eu256) = EU256::abi_decode_validate(&return_value.data)
		{
			eu256.to::<u128>()
		} else {
			0
		}
	}
```

**File:** substrate/frame/revive/src/impl_fungibles.rs (L161-241)
```rust
impl<T: Config> fungibles::Mutate<<T as frame_system::Config>::AccountId> for Pallet<T> {
	fn burn_from(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
		_: Preservation,
		_: Precision,
		_: Fortitude,
	) -> Result<Self::Balance, DispatchError> {
		let checking_account_eth = T::AddressMapper::to_address(&Self::checking_account());
		let checking_address = Address::from(Into::<[u8; 20]>::into(checking_account_eth));
		let data =
			IERC20::transferCall { to: checking_address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, weight_consumed, .. } = Self::bare_call(
			OriginFor::<T>::signed(who.clone()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		log::trace!(target: "whatiwant", "{weight_consumed}");
		if let Ok(return_value) = result {
			if return_value.did_revert() {
				Err("Contract reverted".into())
			} else {
				let is_success =
					bool::abi_decode_validate(&return_value.data).expect("Failed to ABI decode");
				if is_success {
					let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
					Ok(balance)
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}

	fn mint_into(
		asset_id: Self::AssetId,
		who: &T::AccountId,
		amount: Self::Balance,
	) -> Result<Self::Balance, DispatchError> {
		let eth_address = T::AddressMapper::to_address(who);
		let address = Address::from(Into::<[u8; 20]>::into(eth_address));
		let data = IERC20::transferCall { to: address, value: EU256::from(amount) }.abi_encode();
		let ContractResult { result, .. } = Self::bare_call(
			OriginFor::<T>::signed(Self::checking_account()),
			asset_id,
			U256::zero(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: WEIGHT_LIMIT,
				deposit_limit:
					<<T as pallet::Config>::Currency as fungible::Inspect<_>>::total_issuance(),
			},
			data,
			&ExecConfig::new_substrate_tx(),
		);
		if let Ok(return_value) = result {
			if return_value.did_revert() {
				Err("Contract reverted".into())
			} else {
				let is_success =
					bool::abi_decode_validate(&return_value.data).expect("Failed to ABI decode");
				if is_success {
					let balance = <Self as fungibles::Inspect<_>>::balance(asset_id, who);
					Ok(balance)
				} else {
					Err("Contract transfer failed".into())
				}
			}
		} else {
			Err("Contract out of gas".into())
		}
	}
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L430-461)
```rust
	fn add_liquidity(
		call: &IAssetConversion::addLiquidityCall,
		env: &mut impl Ext<T = Runtime>,
	) -> Result<Vec<u8>, Error> {
		env.charge(<Runtime as pallet_asset_conversion::Config>::WeightInfo::add_liquidity())?;

		let asset1 = Self::decode_asset_kind(&call.asset1)?;
		let asset2 = Self::decode_asset_kind(&call.asset2)?;

		let sender = Self::caller_account_id(env)?;
		let mint_to = env.to_account_id(&H160(call.mintTo.0 .0));

		let lp_tokens = <pallet_asset_conversion::Pallet<Runtime> as MutateLiquidity<
			<Runtime as frame_system::Config>::AccountId,
		>>::add_liquidity(
			&sender,
			AddLiquidityAsset {
				asset: asset1,
				amount_desired: Self::to_balance(call.amount1Desired)?,
				amount_min: Self::to_balance(call.amount1Min)?,
			},
			AddLiquidityAsset {
				asset: asset2,
				amount_desired: Self::to_balance(call.amount2Desired)?,
				amount_min: Self::to_balance(call.amount2Min)?,
			},
			&mint_to,
		)?;

		Ok(IAssetConversion::addLiquidityCall::abi_encode_returns(&Self::to_u256(lp_tokens)?))
	}

```
