## Analysis

The ZetaChain bug's core invariant break is: **an unprivileged actor can manipulate the ratio/state of a freshly-seeded liquidity pool before honest participants act on it, extracting value from the resulting imbalance.** The closest verifiable analog in `polkadot-sdk` is in `pallet-asset-conversion` (the in-repo Uniswap-V2-style AMM used by Asset Hub and other runtimes), where the pool's reserves are read directly from a plain, freely-transferable `AccountId` rather than from pallet-tracked internal balances.

### Title
First-Liquidity-Provider Donation/Inflation Attack via Unrestricted Transfers to the Pool Account - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` derives each pool's reserves by reading the live token balance of a deterministic, plain sovereign account (`T::PoolLocator::address`), not from pallet-internal reserve counters. Because this account is an ordinary `AccountId`, *anyone* can transfer tokens into it directly (via `Balances`/`Assets::transfer`) without going through `add_liquidity`. An attacker who is the first liquidity provider (minting the statutory minimal LP amount) can then donate a large amount of one asset straight to the pool account, artificially inflating the on-chain `reserve` used for subsequent share calculations. This is the classic constant-product-AMM "donation/inflation" attack, and it is structurally analogous to the ZetaChain issue: a pool's initial/early state can be skewed by an unprivileged party, and the LP-share math built on top of that skewed state produces a bad, exploitable settlement for later depositors — a direct value-imbalance-then-arbitrage/theft pattern, just enacted through direct token donation instead of a fixed gas-pool seed ratio.

### Finding Description
`do_add_liquidity` computes reserves via `Self::get_balance(&pool_account, asset)` [1](#0-0) , and when `total_supply` (LP token issuance) is non-zero, the LP tokens minted to a new depositor are calculated as:

```
side1 = amount1 * total_supply / reserve1
side2 = amount2 * total_supply / reserve2
lp_token_amount = side1.min(side2)
``` [2](#0-1) 

`reserve1`/`reserve2` here are raw account balances, not a pallet-tracked invariant that only changes through `add_liquidity`/`remove_liquidity`/`swap`. Because the pool account is derivable off-chain before the pool even exists (`PoolLocator::address`), and the pallet's own test suite acknowledges attackers can pre-fund this exact account (`cannot_block_pool_creation`) [3](#0-2) , nothing prevents an attacker from:
1. Creating the pool and providing the minimal first liquidity (getting `MintMinLiquidity` LP tokens locked to the pool account, and a small nonzero amount of real LP tokens to themselves) via `create_pool`/`add_liquidity` [4](#0-3) .
2. Directly transferring (donating) a very large amount of one of the two assets straight into the pool account — bypassing `add_liquidity` entirely, so `total_supply` of LP tokens is unaffected but `reserve1`/`reserve2` balloons.
3. When a legitimate second depositor calls `add_liquidity`, `side1`/`side2` are computed against the now-inflated reserve, causing severe rounding-down of the LP tokens minted to the victim relative to the real value they deposited — the attacker can then `remove_liquidity` and capture a disproportionate share of the victim's contribution.

This is the same broken invariant as the external report: **a pool's early/initial state, cheaply steerable by an unprivileged party, feeds directly into value-critical settlement math with no guard tying LP share issuance to a manipulation-resistant reserve.** The `MintMinLiquidity` constant only mitigates the "zero-supply" bootstrap step, not the very next deposit once `total_supply > 0`.

### Impact Explanation
An unprivileged attacker can extract real economic value from any legitimate liquidity provider who is not aware of, or does not defend against, this inflation pattern — a fund-loss/theft outcome that satisfies "theft or unbacked mint... duplicate settlement or payout" under the Impact Gate. Any parachain runtime that uses `pallet-asset-conversion` (Asset Hub, staking-async parachain runtime, etc., confirmed via multiple runtime configs [5](#0-4) ) with public, permissionless pool creation is exposed.

### Likelihood Explanation
`create_pool` and `add_liquidity` are permissionless, signed-origin extrinsics [6](#0-5) , and the pool account address is computable in advance by anyone via `PoolLocator::address`. Donating tokens is a plain `Balances`/`Assets` transfer requiring no special privilege. Any newly created, low-liquidity pool (exactly the scenario the original report highlights — pools freshly seeded for a newly-supported asset) is a natural target, since attackers can watch for `PoolCreated`/`LiquidityAdded` events and act before a second liquidity provider arrives.

### Recommendation
Track pool reserves as pallet-internal storage state updated only by `add_liquidity`/`remove_liquidity`/`swap`, instead of trusting the pool account's live balance; or, if balance-based reserves must be kept, require that LP-share minting for non-zero total supply be bounded to changes attributable to the caller's own transfer (e.g., compare balances immediately before and after the extrinsic's own transfer, similar to virtual-offset/decimal-padding mitigations used by hardened AMM implementations) to make donation-based reserve inflation ineffective.

### Proof of Concept
1. Attacker calls `create_pool(asset1, asset2)` then `add_liquidity` with a small amount (e.g. `amount1=amount2=101`), receiving `1` real LP token (after `MintMinLiquidity=100` is locked) — see zero-supply path [7](#0-6) .
2. Attacker calls `Assets::transfer`/`Balances::transfer` to send a very large amount of `asset1` (e.g. `1_000_000`) directly to the pool account computed via `PoolLocator::address`.
3. Victim calls `add_liquidity` intending to deposit a fair, proportionate amount of `asset1`/`asset2`; because `reserve1` now reflects the attacker's donation, `side1 = amount1 * total_supply(=1) / reserve1(≈1_000_101)` rounds down to `0`, tripping `Error::InsufficientLiquidityMinted` or minting the victim a negligible LP share for a large real deposit [8](#0-7) .
4. Attacker calls `remove_liquidity` to redeem their `1` LP token, but since LP-token redemption also reads reserves via `get_reserves` [9](#0-8) , the attacker's `1` LP token now represents a proportionate share of the pool's *inflated* asset1 balance plus the victim's freshly deposited asset2 — extracting value contributed by the victim.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L440-450)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::create_pool())]
		pub fn create_pool(
			origin: OriginFor<T>,
			asset1: Box<T::AssetKind>,
			asset2: Box<T::AssetKind>,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_create_pool(&sender, *asset1, *asset2, None)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L661-723)
```rust
	impl<T: Config> Pallet<T> {
		/// Create a pool at genesis, bypassing the setup fee.
		///
		/// The `lp_provider` must already hold sufficient balances of both assets.
		/// If both `amount1` and `amount2` are non-zero, initial liquidity is added.
		/// Returns the LP token amount minted to `lp_provider` (zero if no liquidity).
		pub(crate) fn setup_pool_from_genesis(
			asset1: &T::AssetKind,
			asset2: &T::AssetKind,
			lp_provider: &T::AccountId,
			amount1: T::Balance,
			amount2: T::Balance,
		) -> Result<T::Balance, DispatchError> {
			ensure!(asset1 != asset2, Error::<T>::InvalidAssetPair);

			let pool_id = T::PoolLocator::pool_id(asset1, asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;
			ensure!(!Pools::<T>::contains_key(&pool_id), Error::<T>::PoolExists);

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			// Allocate LP token ID.
			let lp_token = NextPoolAssetId::<T>::get()
				.or(T::PoolAssetId::initial_value())
				.ok_or(Error::<T>::IncorrectPoolAssetId)?;
			let next_lp_token_id = lp_token.increment().ok_or(Error::<T>::IncorrectPoolAssetId)?;
			NextPoolAssetId::<T>::set(Some(next_lp_token_id));

			// Create LP token asset.
			T::PoolAssets::create(lp_token.clone(), pool_account.clone(), false, 1u32.into())?;

			// Touch asset accounts for the pool account.
			if T::Assets::should_touch(asset1.clone(), &pool_account) {
				T::Assets::touch(asset1.clone(), &pool_account, lp_provider)?;
			}
			if T::Assets::should_touch(asset2.clone(), &pool_account) {
				T::Assets::touch(asset2.clone(), &pool_account, lp_provider)?;
			}
			if T::PoolAssets::should_touch(lp_token.clone(), &pool_account) {
				T::PoolAssets::touch(lp_token.clone(), &pool_account, lp_provider)?;
			}

			// Register pool.
			Pools::<T>::insert(pool_id, PoolInfo { lp_token: lp_token.clone() });

			// Add initial liquidity if amounts are non-zero.
			if !amount1.is_zero() && !amount2.is_zero() {
				T::Assets::transfer(asset1.clone(), lp_provider, &pool_account, amount1, Preserve)?;
				T::Assets::transfer(asset2.clone(), lp_provider, &pool_account, amount2, Preserve)?;

				let lp_token_amount = Self::calc_lp_amount_for_zero_supply(&amount1, &amount2)?;
				T::PoolAssets::mint_into(
					lp_token.clone(),
					&pool_account,
					T::MintMinLiquidity::get(),
				)?;
				T::PoolAssets::mint_into(lp_token, lp_provider, lp_token_amount)?;

				Ok(lp_token_amount)
			} else {
				Ok(Zero::zero())
			}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L810-814)
```rust
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-879)
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

			T::PoolAssets::mint_into(pool.lp_token.clone(), mint_to, lp_token_amount)?;
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L900-920)
```rust
			amount1_min_receive: T::Balance,
			amount2_min_receive: T::Balance,
			withdraw_to: &T::AccountId,
		) -> Result<(T::Balance, T::Balance), DispatchError> {
			let pool_id = T::PoolLocator::pool_id(&asset1, &asset2)
				.map_err(|_| Error::<T>::InvalidAssetPair)?;

			ensure!(lp_token_burn > Zero::zero(), Error::<T>::ZeroLiquidity);

			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;

			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;
			let (reserve1, reserve2) = Self::get_reserves(asset1.clone(), asset2.clone())?;

			let total_supply = T::PoolAssets::total_issuance(pool.lp_token.clone());
			let withdrawal_fee_amount = T::LiquidityWithdrawalFee::get() * lp_token_burn;
			let lp_redeem_amount = lp_token_burn.saturating_sub(withdrawal_fee_amount);

			let amount1 = Self::mul_div(&lp_redeem_amount, &reserve1, &total_supply)?;
			let amount2 = Self::mul_div(&lp_redeem_amount, &reserve2, &total_supply)?;
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

**File:** substrate/bin/node/runtime/src/lib.rs (L2032-2067)
```rust
impl pallet_asset_conversion::Config for Runtime {
	type RuntimeEvent = RuntimeEvent;
	type Balance = u128;
	type HigherPrecisionBalance = sp_core::U256;
	type AssetKind = NativeOrWithId<u32>;
	type Assets = NativeAndAssets;
	type PoolId = (Self::AssetKind, Self::AssetKind);
	type PoolLocator = Chain<
		WithFirstAsset<
			Native,
			AccountId,
			NativeOrWithId<u32>,
			AccountIdConverter<AssetConversionPalletId, Self::PoolId>,
		>,
		Ascending<
			AccountId,
			NativeOrWithId<u32>,
			AccountIdConverter<AssetConversionPalletId, Self::PoolId>,
		>,
	>;
	type PoolAssetId = <Self as pallet_assets::Config<Instance2>>::AssetId;
	type PoolAssets = PoolAssets;
	type PoolSetupFee = PoolSetupFee;
	type PoolSetupFeeAsset = Native;
	type PoolSetupFeeTarget = ResolveAssetTo<AssetConversionOrigin, Self::Assets>;
	type PalletId = AssetConversionPalletId;
	type LPFee = LpFee;
	type AdminOrigin = EnsureRoot<AccountId>;
	type MaxSwapFee = MaxSwapFee;
	type LiquidityWithdrawalFee = LiquidityWithdrawalFee;
	type WeightInfo = pallet_asset_conversion::weights::SubstrateWeight<Runtime>;
	type MaxSwapPathLength = ConstU32<4>;
	type MintMinLiquidity = MintMinLiquidity;
	#[cfg(feature = "runtime-benchmarks")]
	type BenchmarkHelper = ();
}
```
