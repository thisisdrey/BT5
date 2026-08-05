## Analysis

The Aerodrome bug's core invariant break is: **an AMM reads live token balances of a pool address as its "reserves," so anyone can permissionlessly inflate those reserves by donating tokens directly to the pool account, without any accounting distinguishing "deposited via protocol" from "just sent in."** Downstream consumers that treat "reserve" as a trusted valuation/price signal are then corrupted.

`pallet-asset-conversion` has the exact same design. `Pallet::get_reserves` computes reserves as the *live* balance of the deterministic pool account rather than an internally tracked ledger: [1](#0-0) 

The pool account itself is just a normal, publicly-derivable `AccountId` (`blake2_256(PalletId, PoolId)`), reachable via ordinary `Balances::transfer`/`Assets::transfer` calls — there is no lock or gate preventing "donations": [2](#0-1) 

The prdoc for `pr_12408` confirms the pallet deliberately reads *full* (not reducible) balances for pricing/liquidity math, i.e. donated tokens are guaranteed to count: [3](#0-2) 

This live-balance reserve is used directly in `add_liquidity`'s LP-mint math, where a floor-division ratio determines newly minted LP tokens relative to existing reserves and `total_supply`: [4](#0-3) 

Because `reserve1`/`reserve2` in that computation are just `Self::get_balance(&pool_account, asset)` — i.e., whatever balance currently sits at the pool account, donated or not — an attacker can inflate reserves relative to LP `total_supply` *without* minting any LP tokens for that donated amount. A subsequent legitimate liquidity provider's `side1 = mul_div(amount1, total_supply, reserve1)` / `side2` then floors toward zero, so they receive far fewer LP tokens than their deposit is worth, while the attacker (holding LP tokens minted before the donation) can later redeem proportionally to the now-inflated reserve, capturing the victim's contributed value. The only guard, `InsufficientLiquidityMinted` (`lp_token_amount > MintMinLiquidity`), only prevents literally-zero mints — it does not prevent severe, attacker-favorable rounding loss for large deposits against inflated reserves, since `MintMinLiquidity` is a small fixed constant (e.g. `100`) regardless of deposit size: [5](#0-4) 

The same manipulated reserve values also feed the price-oracle view functions (`quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens` and the `AssetConversionApi` runtime API), which other pallets (e.g. `pallet-asset-conversion-tx-payment`) or external consumers may rely on as a trusted price feed: [6](#0-5) 

### Title
Direct token donation to the deterministic AMM pool account inflates `get_reserves()`, enabling LP-share dilution theft and price-oracle manipulation - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool "reserves" as the live token balance of a publicly-derivable pool `AccountId`, with no internal ledger separating protocol-recorded deposits from arbitrary incoming transfers. Anyone can pre-compute the pool address via `PoolLocator::address`/`pool_address` and send tokens to it directly (`Balances::transfer`/`Assets::transfer`), instantly and permanently inflating the value returned by `get_reserves()`, `quote_price_exact_tokens_for_tokens()`, `quote_price_tokens_for_exact_tokens()`, and the LP-mint math in `add_liquidity`/`setup_pool_from_genesis` — exactly the "donate + reserves auto-sync" primitive from the Aerodrome report, except here reserves are recomputed live on every read, so no explicit `sync()` analog is even needed.

### Finding Description
`get_reserves` at [1](#0-0)  reads `Self::get_balance(&pool_account, asset)` for both assets — the pool account's actual on-chain balance, not a protocol-tracked internal reserve counter. The pool account address is a pure function of `(PalletId, PoolId)` via `AccountIdConverter::try_convert`, computable and reachable by anyone without any dispatchable call: [2](#0-1) 

`add_liquidity`'s share-minting logic uses these same live balances as `reserve1`/`reserve2` to compute `side1 = mul_div(amount1, total_supply, reserve1)` and `side2` analogously, taking `lp_token_amount = side1.min(side2)`: [7](#0-6) 

Because integer (floor) division is used in `mul_div`/`quote`, inflating `reserve1`/`reserve2` relative to `total_supply` (LP token issuance) via a donation — which does *not* increase `total_supply` — causes any subsequent depositor's minted LP amount to round down disproportionately relative to the value they contribute. The only sanity check, `InsufficientLiquidityMinted` (`lp_token_amount > MintMinLiquidity`), is a small fixed floor (`ConstU128<100>` in test/runtime configs) that does not scale with deposit size and therefore does not prevent large-value rounding loss.

### Impact Explanation
This breaks the "assets conserve value and settle exactly once to the rightful beneficiary and amount" invariant for LP token minting: a victim depositing real tokens can receive LP shares worth far less than their deposit, with the difference effectively transferred to whoever holds LP tokens minted before the donation (the attacker, who donated without spending real economic value since they retain ownership of the donated tokens' claim via their pre-existing LP position). It also corrupts the price-oracle surface (`quote_price_*`, `AssetConversionApi`, and the asset-conversion precompile's `getReserves`) that other pallets/consumers (e.g., asset-based transaction fee payment, or any downstream valuation logic) rely on as ground truth for asset pricing, letting an attacker manipulate the reported exchange rate at will without executing an actual swap.

### Likelihood Explanation
Any unprivileged, signed account can compute the pool address off-chain (deterministic hash) and perform a plain `Balances::transfer`/`Assets::transfer` to it — no governance, admin, validator, or malicious-peer assumption is required. The attack requires only that a pool already exists with some initial (even attacker-controlled) liquidity, which is trivially achievable via `create_pool`/`add_liquidity`.

### Recommendation
Track pool reserves as an explicit, protocol-updated storage value incremented/decremented only during `add_liquidity`, `remove_liquidity`, and `swap` execution, rather than deriving "reserves" from the live queryable balance of the pool account. If live-balance reads must be retained for backward compatibility, cap LP-mint calculations and price quotes to the minimum of (tracked-deposit reserve, live balance) so that undeposited "donated" tokens cannot be counted toward share-minting ratios, and/or require `add_liquidity` to only mint proportional to amounts actually transferred in `T::Assets::transfer` calls in that pallet-controlled flow.

### Proof of Concept
1. Attacker creates a pool for `(asset1, asset2)` via `create_pool`, then calls `add_liquidity` with minimal amounts to receive `lp_token_amount` slightly above `MintMinLiquidity` (e.g., mints `101` LP tokens while depositing a token amount that is negligible relative to what a later victim will deposit) — see mint logic at [4](#0-3) .
2. Attacker computes the pool's `AccountId` via `PoolLocator::address` (public, deterministic) and sends a large amount of `asset1`/`asset2` directly to it using ordinary `Assets::transfer`/`Balances::transfer_allow_death`, bypassing `add_liquidity` entirely — this is exactly what `cannot_block_pool_creation` test demonstrates is possible (direct transfers to the pool account before any pool-owned liquidity accounting occurs): [8](#0-7) .
3. `get_reserves` now reports the inflated balances (attacker's minimal deposit + large donation) while `total_supply` of LP tokens remains at `101` (from step 1).
4. A victim calls `add_liquidity` intending to deposit proportionally; because `reserve1`/`reserve2` are inflated relative to `total_supply`, `side1`/`side2 = mul_div(amount_desired, total_supply, reserve)` round toward a much smaller LP mint than the victim's contribution is worth, while still clearing the fixed `MintMinLiquidity` check.
5. Attacker then calls `remove_liquidity`, redeeming their `101` LP tokens for a share of the now-larger pool (their donation + attacker's original deposit + victim's under-compensated deposit), netting a profit equal to the value the victim under-received in LP shares.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-877)
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

			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
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

**File:** prdoc/pr_12408.prdoc (L1-8)
```text
title: 'fix(asset-conversion): use full balances for pool prices'
doc:
- audience: Runtime Dev
  description: |
    `pallet-asset-conversion` now reads full pool account balances when calculating
    pool prices and liquidity amounts. Previously, these calculations used reducible
    balances, which could understate pool reserves when protected funds or unrelated
    non-sufficient assets were held in the pool account.
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
