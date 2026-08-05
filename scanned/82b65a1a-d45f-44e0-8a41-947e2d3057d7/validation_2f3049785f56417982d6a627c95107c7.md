Based on my investigation, the analog exists in `pallet-asset-conversion`'s liquidity-provisioning logic, where the "expected reserve" used to price a deposit and mint LP tokens is computed directly from the pool account's live token balance rather than from any internally-tracked/expected value — exactly the same class of flaw as the WagmiLeverage report (using the raw contract balance, which is externally inflatable, as the "expected" quantity feeding a strict downstream check).

### Title
Direct-transfer donation to a pool account manipulates reserve-based pricing and can revert/DoS `add_liquidity` and `remove_liquidity` - ([File: substrate/frame/asset-conversion/src/lib.rs])

### Summary
`do_add_liquidity` computes `reserve1`/`reserve2` via `Self::get_balance(&pool_account, asset)` [1](#0-0)  — i.e. it reads the *actual live token balance* held by the pool account rather than any pool-tracked "expected" reserve value. Just as the WagmiLeverage callback used `flashBalance` (the contract's raw token balance, inflatable by donation) instead of a tracked expected amount, this pallet uses the pool account's raw balance as the basis for `quote()`, minimum-deposit checks, and LP-token minting math.

### Finding Description
Any unprivileged account can call `pallet_balances`/`pallet_assets` `transfer` (or `transfer_keep_alive`) directly to a pool's sovereign account (`T::PoolLocator::address(&pool_id)`), which is a well-known, deterministically derivable address, without going through `add_liquidity`/`swap` at all. This changes `reserve1`/`reserve2` observed by `do_add_liquidity` [1](#0-0)  on the very next block/transaction, since the function has no notion of an "expected" or pallet-tracked reserve — it trusts the account's spendable balance entirely.

This live balance then feeds:
- `Self::quote(&amount1_desired, &reserve1, &reserve2)` to compute the "optimal" counter-asset amount [2](#0-1) 
- the strict `ensure!` checks `AssetTwoDepositDidNotMeetMinimum`, `OptimalAmountLessThanDesired`, `AssetOneDepositDidNotMeetMinimum` [3](#0-2) 
- LP-token minting via `Self::mul_div(&amount1, &total_supply, &reserve1)` / `&amount2, &total_supply, &reserve2` [4](#0-3) 

Because a front-runner-independent donation (a plain, unconditional balance transfer sent at any time before the victim's `add_liquidity` extrinsic is included) permanently and directly inflates `reserve1`/`reserve2` (there is no reserve-recompute step, no snapshot, no minimum-balance guard against uninvited transfers), it deterministically:
1. Distorts `quote()` so the victim's desired ratio no longer matches the pool's true price ratio, causing the strict `ensure!` checks (`AssetOneDepositDidNotMeetMinimum` / `AssetTwoDepositDidNotMeetMinimum` / `OptimalAmountLessThanDesired`) to fail on a previously-valid, honestly-computed transaction, and/or
2. Dilutes `lp_token_amount = side1.min(side2)` computed against the inflated reserve, causing `InsufficientLiquidityMinted` to trip and revert the whole extrinsic [5](#0-4) , or silently under-mints LP shares relative to the value actually deposited.

This differs from ordinary front-running/slippage risk in AMMs because the attacker does not need to interact with the swap/liquidity path at all — a bare `Balances::transfer`/`Assets::transfer` to the pool's sovereign account is sufficient, is unconditional (not contingent on any particular victim transaction being pending), and permanently corrupts the reserve baseline the pallet relies on for pricing until a compensating swap/liquidity op re-aligns it. Existing guards (`AmountOneLessThanMinimal`/`AmountTwoLessThanMinimal`, `ReserveLeftLessThanMinimal`) only check that reserves stay above the asset's existential/minimum balance — they do nothing to prevent or detect an inflated reserve baseline, since they are computed from the very same corrupted `get_balance` read.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge/queue processing" and "runtime bugs that compromise intended behavior" for the pool pallet: legitimate `add_liquidity`/`remove_liquidity` calls can be made to revert (denial of service against normal LP flows) or to mint LP shares at an unintended, attacker-influenced ratio, misallocating value between the depositor and existing LPs. Because `pallet-asset-conversion` underlies AssetHub's native fee-conversion and liquidity infrastructure, repeated griefing of this kind degrades usability of a core runtime service without requiring any privileged, validator, or off-chain-relayer role.

### Likelihood Explanation
The attack requires only: (a) knowledge of the deterministic pool account address (derivable by anyone via `T::PoolLocator::address`), and (b) a single ordinary token transfer transaction, executable at any time (not contingent on transaction ordering against a specific victim tx). This is realistic and cheap for any pool with reasonably tight liquidity ratios, making the likelihood moderate-to-high wherever pools are shallow or newly created.

### Recommendation
Do not derive pricing/minting inputs from the pool account's raw spendable balance. Track reserves explicitly in pallet storage (updated only through `add_liquidity`/`remove_liquidity`/`swap` code paths) and use that tracked value for `quote()`, the deposit-minimum checks, and LP-token minting math, reconciling any donated/untracked balance separately (e.g., treat it as unclaimed dust rather than part of the priced reserve).

### Proof of Concept
1. Attacker creates or targets an existing shallow pool for asset pair `(A, B)` and derives its sovereign account via `PoolLocator::address(&pool_id)`.
2. Attacker sends a plain `Assets::transfer` (or `Balances::transfer`) of asset `A` directly to the pool account — no interaction with `pallet-asset-conversion` extrinsics at all.
3. This inflates `reserve1` read by `get_balance(&pool_account, asset1)` in `do_add_liquidity` [6](#0-5) .
4. Victim submits a normal `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, ...)` extrinsic sized against the pool's previously observed (pre-donation) ratio.
5. `Self::quote(&amount1_desired, &reserve1, &reserve2)` [7](#0-6)  now returns a skewed `amount2_optimal` relative to the victim's actual holdings/expectations, tripping `AssetTwoDepositDidNotMeetMinimum` or `AssetOneDepositDidNotMeetMinimum`/`OptimalAmountLessThanDesired`, reverting the victim's transaction (DoS), or passing through with a distorted `lp_token_amount = side1.min(side2)` [8](#0-7)  that mismints LP shares relative to the value the victim actually contributed.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-814)
```rust
			let reserve1 = Self::get_balance(&pool_account, asset1.clone());
			let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L821-843)
```rust
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L868-872)
```rust
			} else {
				let side1 = Self::mul_div(&amount1, &total_supply, &reserve1)?;
				let side2 = Self::mul_div(&amount2, &total_supply, &reserve2)?;
				lp_token_amount = side1.min(side2);
			}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L874-877)
```rust
			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```
