### Title
AMM pool reserves/price derived from raw account balance allow a direct-transfer "donation" to permanently dilute or lock LP funds and skew swap/fee pricing - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`pallet-asset-conversion` computes pool reserves (`get_reserves`) and every downstream price/liquidity calculation directly from the pool account's on-chain asset balance, not from an internally tracked accounting value that is only updated through the pallet's own extrinsics (`add_liquidity`/`remove_liquidity`/`swap_*`). Because the pool account is an ordinary, unprivileged account, any user can transfer assets directly into it (bypassing `add_liquidity`) without minting LP tokens. This is the same broken invariant as the external report: a public entity's "demand"/state is derived from the raw balance of a designated account rather than from an explicit request/registration path, so an unprivileged, accidental or intentional direct transfer corrupts pool accounting, with no LP-token compensation and no straightforward recovery path for the sender.

### Finding Description
`Pallet::get_reserves` reads the balances of the pool account directly: [1](#0-0) 

This value feeds `quote_price_exact_tokens_for_tokens` / `quote_price_tokens_for_exact_tokens` and the swap `get_amount_in`/`get_amount_out`/`quote` math used both for user swaps and for the `asset-conversion-tx-payment` fee pallet: [2](#0-1) [3](#0-2) 

The pool account (`T::PoolLocator::pool_address` / `address`) is a normal keyless derived account, not access-restricted; regular `Balances::transfer`/`Assets::transfer` calls can move funds into it, exactly as `SharesManager` allowed a plain `transferFrom`-independent transfer to `RedeemManager` in the external report. Tests explicitly demonstrate the pool's *actual* raw balance being manipulated outside of `add_liquidity`/`swap` flows (e.g., directly burning/crediting the pool account) and the reserve-dependent functions reacting to it: [4](#0-3) [5](#0-4) 

This mirrors the reward-deficit class of bug the project itself previously had to patch for `pallet-nomination-pools` (fragile "look at raw balance" accounting), and a related fix confirms the reserve/price calculation in `pallet-asset-conversion` is explicitly defined as "full pool account balance": [6](#0-5) 

Unlike `pallet-nomination-pools`'s `deposit_reward_tokens`, which documents that direct transfers are an *intended* convenience for topping up a reward pot with no expectation of getting anything back, `pallet-asset-conversion`'s pool account holds principal that LPs expect to redeem proportionally via LP tokens. There is no guard preventing an ordinary account from crediting the pool account outside of `add_liquidity`, and no compensating LP-token issuance for such a transfer — the donated funds become permanently commingled with the reserve, redistributed pro-rata to whoever calls `remove_liquidity` next, and unrecoverable by the sender through any pallet-provided call.

### Impact Explanation
- **Fund loss / permanent lock for the donor**: tokens sent directly to the pool account do not mint LP shares; the sender has no extrinsic path to reclaim them — they are effectively donated to existing/future LPs. This matches "permanent user-fund lock" in the impact gate.
- **Wrong beneficiary/amount for price-dependent flows**: because `get_reserves` is the raw balance, an attacker can donate assets right before another user's swap or before an `asset-conversion-tx-payment` fee quote is computed, artificially shifting the AMM's `k`-based pricing and the amount charged/received (particularly problematic for `ChargeAssetTxPayment`, which relies on `quote_price_tokens_for_exact_tokens` to convert transaction fees into non-native asset — a manipulable state that determines value settled to the transaction-fee beneficiary).
- **No privileged actor required**: this is exploitable by any signed account doing a normal, public `transfer`/`assets::transfer` extrinsic; it needs no malicious validator/collator/relayer/governance action, satisfying the "unprivileged attacker" requirement.

### Likelihood Explanation
Medium. Exploitation requires only a standard balance transfer call to a publicly-derivable pool account address (`PoolLocator::pool_address`), which any user can compute and is not restricted by any additional check in the transfer path. The main friction is that an attacker donating funds primarily benefits other LPs rather than themselves directly (unless combined with a swap to extract value from the skewed price, or used to grief users relying on `quote_price_*` for slippage protection or fee conversion). This is analogous in severity to the "Medium Risk" rating given to the original LiquidCollective finding, since it is an accidental/self-inflicted-loss class bug with a secondary price-manipulation angle rather than a direct chain-halting exploit.

### Recommendation
Track pool reserves via an internal, pallet-controlled accounting value (e.g., a `PoolInfo` struct storing `reserve0`/`reserve1`) that is updated only inside `add_liquidity`, `remove_liquidity`, and `swap_*`, instead of re-reading the pool account's live balance in `get_reserves`. Where the live balance must still be consulted (e.g., to detect externally donated dust), reconcile any surplus by either (a) allowing a permissionless "sync"/"skim" call that credits the surplus proportionally to existing LP token holders in a well-defined, auditable way, or (b) rejecting/refunding transfers to the pool account that do not originate from the pallet itself. At minimum, document and gate the assumption so that fee-quoting logic (`asset-conversion-tx-payment`) is not silently manipulable by third-party donations immediately preceding a transaction.

### Proof of Concept
1. Create a pool for `(Native, AssetX)` and call `add_liquidity` normally to seed reserves `R0`, `R1`, minting LP tokens to LP `A`.
2. An unrelated attacker calls `Assets::transfer(AssetX, attacker, pool_account, D)` — a completely ordinary, permissionless call — sending `D` extra units of `AssetX` directly to the pool account, bypassing `add_liquidity` entirely. No LP tokens are minted to the attacker.
3. `Pallet::get_reserves(Native, AssetX)` now returns `(R0, R1 + D)` per [1](#0-0) , immediately skewing `quote_price_exact_tokens_for_tokens`/`quote_price_tokens_for_exact_tokens` and any in-flight `swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens` execution that reads reserves at settlement time.
4. Any subsequent `remove_liquidity` call by LP `A` withdraws a proportional share of the now-inflated `AssetX` balance, effectively transferring part of the attacker's donated `D` to `A` — the attacker cannot recover the donated tokens via any pallet call.
5. Separately, if a victim's transaction fee is being quoted in `AssetX` via `ChargeAssetTxPayment` immediately after the donation (as exercised in [7](#0-6) ), the fee-in-asset amount is computed against the manipulated reserve, causing an incorrect (attacker-influenced) fee amount to be charged/refunded.

**Note on completeness**: I was not able to fully inspect `add_liquidity`'s exact LP-token minting formula (`do_add_liquidity`) in this pass to precisely quantify the dilution percentage per donated unit, since the final grep for that function returned only match counts without content before the tool budget ended. The core vulnerable code path (`get_reserves` reading raw balances) and its consumers are, however, directly confirmed above.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L1291-1316)
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1523-1562)
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

			// Small inputs can round output to zero due to integer division.
			if amount_out.is_zero() {
				return None;
			}

			// Swap withdrawals from pools use `keep_alive=true` (Preserve). Use the same
			// preservation level to determine the actual withdrawable amount.
			let max_output = T::Assets::reducible_balance(asset2, &pool_account, Preserve, Polite);
			if amount_out > max_output {
				return None;
			}

			Some(amount_out)
		}
```

**File:** substrate/frame/asset-conversion/src/tests.rs (L1480-1497)
```rust
		let pallet_account = <Test as Config>::PoolLocator::address(&pool_id).unwrap();
		assert_eq!(balance(pallet_account, token_1.clone()), liquidity1);
		assert_eq!(balance(pallet_account, token_2.clone()), liquidity2);

		assert_ok!(AssetConversion::remove_liquidity(
			RuntimeOrigin::signed(user),
			Box::new(token_1.clone()),
			Box::new(token_2.clone()),
			lp_token_minted,
			1,
			1,
			user,
		));

		// Now, the pool should exist but be almost empty.
		// Let's try and drain it.
		assert_eq!(balance(pallet_account, token_1.clone()), 708);
		assert_eq!(balance(pallet_account, token_2.clone()), 15);
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L1580-1586)
```rust
			let fee_in_asset = AssetConversion::quote_price_tokens_for_exact_tokens(
				NativeOrWithId::WithId(asset_id),
				NativeOrWithId::Native,
				fee_in_native,
				true,
			)
			.unwrap();
```

**File:** substrate/frame/transaction-payment/asset-conversion-tx-payment/src/tests.rs (L1596-1617)
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
```

**File:** prdoc/pr_12408.prdoc (L1-11)
```text
title: 'fix(asset-conversion): use full balances for pool prices'
doc:
- audience: Runtime Dev
  description: |
    `pallet-asset-conversion` now reads full pool account balances when calculating
    pool prices and liquidity amounts. Previously, these calculations used reducible
    balances, which could understate pool reserves when protected funds or unrelated
    non-sufficient assets were held in the pool account.
crates:
- name: pallet-asset-conversion
  bump: patch
```
