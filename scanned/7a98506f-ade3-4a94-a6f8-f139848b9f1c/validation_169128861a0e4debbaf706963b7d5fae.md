Based on my investigation, I found a real local analog in `pallet-asset-conversion`.

### Title
Pre-fundable pool account lets a griefer permanently DOS first-liquidity provision in `pallet-asset-conversion` - (File: substrate/frame/asset-conversion/src/lib.rs, substrate/frame/asset-conversion/src/types.rs)

### Summary
`pallet-asset-conversion` derives a pool's `AccountId` purely from its `AssetKind` pair via a deterministic hash (`AccountIdConverter`), with no dependency on a nonce or on the pool having been created yet. Any user can therefore compute the future pool account for any asset pair before `create_pool`/`add_liquidity` is ever called, and pre-fund (donate) both assets to that address. This is the same root cause as the `SuperPool` issue (deterministic, pre-computable target address for a to-be-created pool combined with reserve-dependent share/amount math), but here it is *stronger*: the address depends only on the public asset identifiers, not on a deployer nonce, so no front-running of a specific transaction is even required — the attacker can salt the account well ahead of time for any pair they expect will be created.

### Finding Description
`AccountIdConverter::try_convert` computes the pool account as `blake2_256(Seed, PoolId)` where `PoolId` is just the ordered pair of `AssetKind`s [1](#0-0) . This account is fully known before `create_pool` is ever dispatched, since it depends only on public asset identifiers.

In `do_add_liquidity`, the first-liquidity-provision path branches on whether either reserve is zero: [2](#0-1) 
If an attacker donates directly to the pool account (transfer, not through `add_liquidity`) for **both** assets before the legitimate first liquidity provider calls `add_liquidity`, `reserve1` and `reserve2` are both non-zero even though `total_supply` (LP token issuance) is still zero. This routes execution into the `quote`-based branch instead of the "seed the pool" branch, so the user's actual debited `amount1`/`amount2` are computed against an attacker-chosen ratio rather than the user's desired amounts, causing legitimate deposits to revert with `AssetTwoDepositDidNotMeetMinimum` / `OptimalAmountLessThanDesired` / `AssetOneDepositDidNotMeetMinimum` [3](#0-2) .

Because `total_supply.is_zero()` still triggers the "seed" LP-mint calculation (`calc_lp_amount_for_zero_supply`) using the now-distorted `amount1`/`amount2`, and the result is gated by `ensure!(lp_token_amount > T::MintMinLiquidity::get(), Error::<T>::InsufficientLiquidityMinted)` [4](#0-3) , an attacker who donates a large, disproportionate amount to only one side of the pool can force any well-intentioned deposit size to fail this minimum-liquidity check — exactly mirroring the Sherlock report's dead-share-inflation mechanic, except the "1000 dead shares" role is played by `MintMinLiquidity` (`100`) and the "burn address transfer" is a normal `transfer` to the deterministic pool account.

Existing guards do not stop this: `do_create_pool` never checks or zeroes out pre-existing balances at `pool_account` before inserting `PoolInfo` [5](#0-4) , and `do_add_liquidity` reads live balances via `get_balance` unconditionally [6](#0-5) .

### Impact Explanation
This breaks the intended flow "create pool → provide first liquidity" for any specific asset pair repeatedly: an attacker only needs a tiny amount of both underlying assets (well below what is needed to fund an entire pool at a sane ratio) donated straight to the deterministic, pre-computable pool address. Every attempt to seed the pool at a "normal" ratio can be made to revert (`InsufficientLiquidityMinted` or the deposit-minimum errors), permanently discouraging or blocking creation of that pool. This matches "breaks core contract functionality" for a permissionless, user-driven pool-creation feature, aligned with the "public underpriced work that degrades... stalls... processing" and "permanent user-fund or bridge-state lock"-adjacent DoS impact class in scope.

### Likelihood Explanation
No privileged actor, front-running, relayer, or validator collusion is required. The attacker only needs to know (or guess) which asset pair a market participant plans to bootstrap and can pre-fund the deterministic pool address at any time in advance — not even a same-block front-run is needed, unlike the original `SuperPool` report. The attack cost is bounded by small token amounts (existential-deposit-level), and can be repeated for any new pool.

### Recommendation
Do not derive the acceptance/mint math for the first liquidity provision from live balances at the pool account. Track pool reserves in dedicated pallet storage (updated only through `do_add_liquidity`/`do_remove_liquidity`/swap logic) rather than trusting `T::Assets::balance(asset, &pool_account)`, or alternatively sweep/burn any un-attributed balance at `pool_account` at `create_pool` time before allowing the first `add_liquidity` to proceed, similar to hardcoding "dead shares" instead of requiring them to be earned from a griefable balance read.

### Proof of Concept
1. Compute `pool_account = AccountIdConverter::try_convert(&(asset1, asset2))` for a target asset pair (public, deterministic, no on-chain state needed).
2. Attacker transfers a small amount of `asset1` and a disproportionate amount of `asset2` directly to `pool_account` (plain `transfer`, not through the pallet).
3. Victim calls `create_pool(asset1, asset2)` — succeeds, `Pools` storage inserted, `total_supply` of `lp_token` still 0.
4. Victim calls `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, ...)` with a normal, balanced amount.
5. Because `reserve1`/`reserve2` are both non-zero (from step 2), execution takes the `quote` branch instead of seeding at the user's desired ratio; the resulting deposit either reverts on `AssetTwoDepositDidNotMeetMinimum`/`OptimalAmountLessThanDesired` or mints `lp_token_amount <= MintMinLiquidity`, reverting with `InsufficientLiquidityMinted` (see existing test pattern at [7](#0-6)  for the mechanics of `InsufficientLiquidityMinted`).
6. The victim must guess and pre-empt the attacker's chosen ratio to succeed, and the attacker can repeat the donation after every failed/reverted attempt at negligible cost.

### Citations

**File:** substrate/frame/asset-conversion/src/types.rs (L147-158)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L740-787)
```rust
			// prepare pool_id
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// pay the setup fee
			let fee =
				Self::withdraw(T::PoolSetupFeeAsset::get(), creator, T::PoolSetupFee::get(), true)?;
			T::PoolSetupFeeTarget::on_unbalanced(fee);

			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, creator)?
			};

			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, creator)?
			};

			let lp_token = NextPoolAssetId::<T>::get()
				.or(T::PoolAssetId::initial_value())
				.ok_or(Error::<T>::IncorrectPoolAssetId)?;
			let next_lp_token_id = lp_token.increment().ok_or(Error::<T>::IncorrectPoolAssetId)?;
			NextPoolAssetId::<T>::set(Some(next_lp_token_id));

			T::PoolAssets::create(lp_token.clone(), pool_account.clone(), false, 1u32.into())?;
			if T::PoolAssets::should_touch(lp_token.clone(), &pool_account) {
				T::PoolAssets::touch(lp_token.clone(), &pool_account, creator)?
			};

			let pool_info = PoolInfo { lp_token: lp_token.clone() };
			Pools::<T>::insert(pool_id.clone(), pool_info);

			Self::deposit_event(Event::PoolCreated {
				creator: creator.clone(),
				pool_id: pool_id.clone(),
				pool_account,
				lp_token,
			});

			if let Some(fee) = initial_fee {
				PoolFees::<T>::insert(&pool_id, fee);
				Self::deposit_event(Event::PoolFeeSet { pool_id: pool_id.clone(), fee });
			}

			Ok(pool_id)
```

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-877)
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

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L422-466)
```rust
#[test]
fn add_tiny_liquidity_leads_to_insufficient_liquidity_minted_error() {
	new_test_ext().execute_with(|| {
		let user = 1;
		let token_1 = NativeOrWithId::Native;
		let token_2 = NativeOrWithId::WithId(2);

		create_tokens(user, vec![token_2.clone()]);
		assert_ok!(AssetConversion::create_pool(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone())
		));

		assert_ok!(Balances::force_set_balance(RuntimeOrigin::root(), user, 1000));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(user), 2, user, 1000));

		assert_noop!(
			AssetConversion::add_liquidity(
				RuntimeOrigin::signed(user),
				Box::new(token_1.clone()),
				Box::new(token_2.clone()),
				1,
				1,
				1,
				1,
				user
			),
			Error::<Test>::AmountOneLessThanMinimal
		);

		assert_noop!(
			AssetConversion::add_liquidity(
				RuntimeOrigin::signed(user),
				Box::new(token_1.clone()),
				Box::new(token_2.clone()),
				get_native_ed(),
				1,
				1,
				1,
				user
			),
			Error::<Test>::InsufficientLiquidityMinted
		);
	});
```
