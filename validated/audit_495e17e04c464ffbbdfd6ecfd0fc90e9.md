### Title
Fungible/Fungibles `Mutate::transfer` default implementation credits the requested amount instead of the actually-debited amount, corrupting `pallet-asset-conversion` pool accounting - ([File: substrate/frame/support/src/traits/tokens/fungibles/regular.rs])

### Summary
The external report's core broken invariant is: a contract records a deposit as the *nominal* amount requested, but only credits its internal accounting (`portfolioTokenBalances`) with that nominal value instead of verifying the actual amount that reached the contract, since the transfer primitive it relies on does not strictly enforce "amount requested == amount moved." The Polkadot SDK analog is the default `Mutate::transfer` implementation shared by `fungible::regular::Mutate` and `fungibles::regular::Mutate`, which is inherited by `pallet-assets` and used directly by `pallet-asset-conversion`'s liquidity/swap accounting.

### Finding Description
The default trait method: [1](#0-0) 

```
fn transfer(asset, source, dest, amount, preservation) -> Result<Self::Balance, DispatchError> {
    let _extra = Self::can_withdraw(asset.clone(), source, amount).into_result(preservation != Expendable)?;
    Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
    if source == dest { return Ok(amount); }

    Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
    // This should never fail as we checked `can_deposit` earlier. But we do a best-effort anyway.
    let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
    Self::done_transfer(asset, source, dest, amount);
    Ok(amount)
}
```

and the identical single-asset variant: [2](#0-1) 

Both discard the `Result<Self::Balance, _>` actually returned by `decrease_balance` (the amount truly debited from `source`, which is explicitly documented as possibly differing from the requested `amount` — see the doc comment on `decrease_balance` a few lines above: "the returned amount may be up to `minimum_balance()-1` greater than `amount`") and instead unconditionally call `increase_balance(dest, amount, BestEffort)` with the *original requested* `amount`, not the actual amount removed from `source`. The function then reports `Ok(amount)` — the nominal value — as the "amount transferred," which is exactly the "record what was requested, not what actually moved" pattern from the report.

`pallet-assets` implements `fungibles::Unbalanced::decrease_balance`/`increase_balance` directly but does not override `fungibles::Mutate::transfer`, so it inherits this default. `pallet-asset-conversion`'s `do_add_liquidity`/`do_remove_liquidity` invoke this exact `transfer` entry point and then compute pool state (reserves, LP token minted amount) from the *nominal* amounts, not from a verified post-transfer balance delta: [3](#0-2) [4](#0-3) 

Just like `RenovaQuest`'s `portfolioTokenBalances` being incremented by the `TokenDeposit.amount` field instead of the balance delta actually observed after `safeTransferFrom`, `do_add_liquidity` computes `lp_token_amount` (the pool-share credited to the depositor) purely from `amount1`/`amount2` (the desired/nominal deposit), and `T::Assets::transfer` is trusted to have moved exactly that much into the pool account — but the underlying primitive it calls (`transfer`) does not tie the destination credit to the source's actual debit.

### Impact Explanation
If the source account's effective debit under `decrease_balance`'s `BestEffort`/`Polite` semantics ever diverges from the nominal `amount` (e.g. dust-removal reaping additional balance, or any asset/account state where `reducible_balance` differs from what the earlier `can_withdraw` pre-check assumed), the pool account can receive less than the nominal transferred amount while the depositor is still minted LP tokens computed for the full nominal amount, and reserves math (`get_balance` on next call) will disagree with the amount credited during `do_add_liquidity`. This directly maps to the report's "users trade with more than actually reached the contract" and "some users unable to withdraw their rightful proportion" impacts — permanent value mismatch/fund lock in liquidity-pool accounting, which is in-scope as "asset accounting" and "conserve value / settle exactly once."

### Likelihood Explanation
This is a structural code defect reachable purely by any unprivileged user calling the public `add_liquidity`/`remove_liquidity`/swap extrinsics on `pallet-asset-conversion` — no malicious peer, validator, governance, or leaked key is required. However, I was not able to fully confirm (due to tool-call limits) whether `pallet-assets`'/`pallet-balances`' concrete `can_withdraw`/`reducible_balance` implementations ever actually diverge from `decrease_balance`'s real effect under currently supported configurations (freezers, holds, consumer refs) in a way that triggers the divergence in practice; in the "normal" case the pre-check and the debit agree and the bug is latent/non-triggering. This uncertainty means the likelihood of a currently-exploitable divergence is unconfirmed and would need further investigation (e.g., fuzzing `decrease_balance` vs. `can_withdraw` consistency across all `Freeze`/`Hold` combinations) before treating this as immediately exploitable.

### Recommendation
Change the default `Mutate::transfer` implementations (`fungible::regular.rs` and `fungibles::regular.rs`) to use the *actual* value returned by `decrease_balance` when calling `increase_balance`, and return that actual value from `transfer`, rather than the originally requested `amount`. `pallet-asset-conversion`'s `do_add_liquidity`/`do_remove_liquidity` should also use the value actually returned by `T::Assets::transfer` (rather than the pre-computed `amount1`/`amount2`) when computing LP token mint amounts and reserve deltas, matching the report's recommendation to "implement an accounting system that appropriately records the amounts that actually get sent."

### Proof of Concept
Could not be fully constructed within the available tool budget: reproducing a real divergence requires identifying a concrete `Freeze`/`Hold`/consumer-ref configuration in `pallet-assets` or `pallet-balances` where `can_withdraw`'s pre-check result differs from `decrease_balance`'s actual reduction under `Precision::BestEffort`. This is flagged as an open verification step for a background engineer: write a unit test in `substrate/frame/asset-conversion/src/tests.rs` that sets up an account with a freeze/hold near the transfer amount, calls `add_liquidity`, and asserts whether `LiquidityAdded.amount1_provided` (nominal) matches the actual post-call balance delta of the pool account and the depositor.

### Citations

**File:** substrate/frame/support/src/traits/tokens/fungibles/regular.rs (L366-386)
```rust
	fn transfer(
		asset: Self::AssetId,
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(asset.clone(), source, amount)
			.into_result(preservation != Expendable)?;
		Self::can_deposit(asset.clone(), dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(asset.clone(), source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(asset.clone(), dest, amount, BestEffort);
		Self::done_transfer(asset, source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/support/src/traits/tokens/fungible/regular.rs (L321-339)
```rust
	fn transfer(
		source: &AccountId,
		dest: &AccountId,
		amount: Self::Balance,
		preservation: Preservation,
	) -> Result<Self::Balance, DispatchError> {
		let _extra = Self::can_withdraw(source, amount).into_result(preservation != Expendable)?;
		Self::can_deposit(dest, amount, Extant).into_result()?;
		if source == dest {
			return Ok(amount);
		}

		Self::decrease_balance(source, amount, BestEffort, preservation, Polite)?;
		// This should never fail as we checked `can_deposit` earlier. But we do a best-effort
		// anyway.
		let _ = Self::increase_balance(dest, amount, BestEffort);
		Self::done_transfer(source, dest, amount);
		Ok(amount)
	}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L813-856)
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
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L858-892)
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

			Self::deposit_event(Event::LiquidityAdded {
				who: who.clone(),
				mint_to: mint_to.clone(),
				pool_id,
				amount1_provided: amount1,
				amount2_provided: amount2,
				lp_token: pool.lp_token,
				lp_token_minted: lp_token_amount,
			});

			Ok(lp_token_amount)
		}
```
