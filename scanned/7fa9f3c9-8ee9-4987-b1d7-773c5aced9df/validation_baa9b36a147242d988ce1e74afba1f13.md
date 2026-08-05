## Analysis

I found a real local analog of the H-01 bug class. The core broken invariant in the external report is: **a function reads the entire live balance of an address to decide how much value to commit, instead of using a value tracked/passed through the call, and that live balance can be inflated by funds arriving from an untracked/unrelated path between the point where the user set their slippage bound and the point where the balance is actually read.**

The exact analog exists in `pallet-asset-conversion`'s `do_add_liquidity`: [1](#0-0) 

```rust
let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
let pool_account =
    T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

let reserve1 = Self::get_balance(&pool_account, asset1.clone());
let reserve2 = Self::get_balance(&pool_account, asset2.clone());
```

`reserve1`/`reserve2` are the pool account's *entire live balance* at execution time, exactly mirroring `PSMBalance = PSM.balanceOf(address(this))` in the AdapterV1 report. This value is then used to compute the *optimal* deposit amount for the counter-asset via `Self::quote(...)` [2](#0-1)  and the `amount1_min`/`amount2_min` slippage checks are evaluated against this same live-read balance [3](#0-2) .

### Why this is exploitable without any privileged actor

The pool account address returned by `T::PoolLocator::address(&pool_id)` is a **deterministic, publicly computable `AccountId`** derived only from `(asset1, asset2)` — it is not a privileged or hidden account. Because it is a normal `AccountId`, any unprivileged account can send plain `Balances::transfer`/`Assets::transfer` extrinsics directly to that address at any time, completely outside the `pallet-asset-conversion` extrinsics. Nothing in `do_add_liquidity` distinguishes "funds transferred in via legitimate swap/liquidity flows" from "funds donated directly to the account by an outsider" — both are folded into the single live balance read by `get_balance`.

This is the same shape as the H-01 flaw: the 1inch adapter also had a pool/adapter-held balance that could silently grow from an untracked external event (partial-fill refund) before the balance was consumed to compute liquidity amounts, defeating the caller's slippage bound.

### Attack path (public entrypoint, unprivileged)

1. Alice submits `add_liquidity(asset1, asset2, amount1_desired, amount2_desired, amount1_min, amount2_min, mint_to)` expecting the pool's current reserves.
2. Before Alice's extrinsic executes (same block, since an unprivileged actor can front-load their own extrinsic earlier in block construction, or across blocks if they see the pending call), an attacker sends a plain transfer of `asset1` (or `asset2`) directly to the pool account (`T::PoolLocator::address(&pool_id)`).
3. When `do_add_liquidity` executes, `reserve1`/`reserve2` are read fresh and now include the attacker's donation. `Self::quote` recomputes `amount2_optimal` based on this skewed ratio, and Alice is forced to either deposit a different, worse ratio of assets than she intended, or her `amount1_min`/`amount2_min` slippage checks fail unpredictably — exactly the "inflated/unfair deposit due to unaccounted balance change" impact described in H-01, applied to a Substrate AMM instead of the Ramses/1inch adapter.
4. This does not require a malicious validator/collator/relayer — only an unprivileged account capable of sending a normal `transfer` extrinsic, which is exactly the class of attacker this exercise requires.

### Title
Untracked pool-account balance inflation in `pallet-asset-conversion::do_add_liquidity` bypasses user slippage protection - (File: `substrate/frame/asset-conversion/src/lib.rs`)

### Summary
`do_add_liquidity` computes deposit ratios and enforces `amount1_min`/`amount2_min` slippage bounds using `reserve1`/`reserve2`, which are read as the pool account's entire live balance (`Self::get_balance`) at execution time rather than a value tracked from when the caller's `amount_desired`/`amount_min` were chosen. Because the pool account address is deterministic and public, anyone can inflate this balance via an ordinary transfer before the `add_liquidity` extrinsic executes, skewing the computed optimal amount and defeating the caller's intended slippage protection — the same root cause as H-01's `PSM.balanceOf(address(this))` issue.

### Finding Description
`T::PoolLocator::address(&pool_id)` returns a deterministic `AccountId` for every asset pair, computable by anyone off-chain [4](#0-3) . `do_add_liquidity` then reads `reserve1`/`reserve2` as this account's current asset balances [5](#0-4)  and uses them to compute `amount2_optimal`/`amount1_optimal` via `Self::quote` and to validate the caller's `amount1_min`/`amount2_min` [2](#0-1) . There is no mechanism ensuring this balance reflects only funds moved through pallet-controlled paths (swaps/prior `add_liquidity` calls) — a plain `Balances::transfer`/`Assets::transfer` to that address changes `reserve1`/`reserve2` exactly like the 1inch refund changed `PSMBalance` in the external report.

### Impact Explanation
A user's `add_liquidity` call can be forced into depositing an unexpected ratio of assets, or the transaction can revert due to `AssetTwoDepositDidNotMeetMinimum`/`AssetOneDepositDidNotMeetMinimum`/`OptimalAmountLessThanDesired`, or worse, succeed at a materially worse ratio if the caller set loose `amount_min` bounds not anticipating balance donation between submission and execution. This degrades the intended behavior of a public, permissionless liquidity-provision entrypoint and can be used to grief or extract value from liquidity providers with no elevated privileges required.

### Likelihood Explanation
High from a mechanics standpoint: the pool account address is derivable by anyone, a plain transfer is all that's needed, and no special timing beyond normal transaction ordering/mempool visibility is required. The action does not need a malicious validator/collator — it only needs a normal user sending a transfer to a known address, which satisfies the "unprivileged attacker" and "public dispatch" requirements.

### Recommendation
Do not use the live full balance of the pool account as the authoritative reserve figure for slippage-sensitive calculations. Track reserves explicitly in pallet storage (updated only through the pallet's own swap/add/remove-liquidity logic) and use that tracked value instead of `T::Assets::balance`/`get_balance` reads of the raw account, or reconcile/reject any balance drift from untracked deposits before performing the ratio calculation.

### Proof of Concept
1. Create pool `(asset1, asset2)`, note the deterministic `pool_account = T::PoolLocator::address(&pool_id)`.
2. Alice submits `add_liquidity(asset1, asset2, amount1_desired=1000, amount2_desired=1000, amount1_min=990, amount2_min=990, mint_to=alice)`.
3. Before this extrinsic executes, attacker submits `Assets::transfer(asset1, attacker, pool_account, X)` (a large, arbitrary donation of `asset1` to `pool_account`).
4. When Alice's extrinsic executes, `reserve1` includes the attacker's `X`, skewing `quote(amount1_desired, reserve1, reserve2)`, producing an `amount2_optimal` far from what Alice expected; her min-bound check either fails (`ProvidedMinimumNotSufficientForSwap`/`AssetTwoDepositDidNotMeetMinimum`) or passes at an unfavorable ratio, matching the "inflated/unfair slippage" impact described in H-01 [2](#0-1) .

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L809-843)
```rust
			let pool = Pools::<T>::get(&pool_id).ok_or(Error::<T>::PoolNotFound)?;
			let pool_account =
				T::PoolLocator::address(&pool_id).map_err(|_| Error::<T>::InvalidAssetPair)?;

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
```
