### Title
Nomination pool points are issued from the nominal `amount` before the funds are actually moved, allowing a value mismatch between points and pool assets — (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The nomination-pools pallet computes and commits pool points using the *nominal* `amount` parameter supplied to `join`/`bond_extra`/`create` **before** the underlying currency transfer/bond operation actually executes and confirms how much value the pool account received. This is the same root-cause pattern as the reported TokensFarm bug: an accounting field (`stake.amount` there, pool `points`/`TotalValueLocked` here) is derived from the requested/nominal value instead of the value actually settled into the account, so any transfer-time discrepancy is silently absorbed into the accounting rather than reflected in it.

### Finding Description
In `BondedPool::try_bond_funds` [1](#0-0) , points are issued via `self.issue(amount)` — which calls `balance_to_point` using the *current* bonded balance and the *nominal* `amount` [2](#0-1)  — strictly before `T::StakeAdapter::pledge_bond(...)` performs the actual transfer/bond of `amount`. `TotalValueLocked` is also incremented by the same nominal `amount` right after, regardless of what `pledge_bond` actually moved [3](#0-2) .

The actual token movement happens in `StakeStrategy::pledge_bond`, e.g. `TransferStake::pledge_bond`, which calls `T::Currency::transfer(&who.0, &pool_account.0, amount, Preservation::...)` and then `Staking::bond(&pool_account.0, amount, ...)` using the *same* nominal `amount` again, without ever reading back how much value the pool account actually gained [4](#0-3) .

`T::Currency` is a generic associated type (a `fungible` trait implementation), not hard-wired to vanilla `pallet-balances`. Any runtime that configures the pool's `Currency`/`StakeAdapter` with an asset or wrapper that can deliver less than the nominal amount to the destination (transfer tax/fee-on-transfer assets, rebasing/deflationary tokens, an asset-conversion wrapper, or a custom `fungible::Mutate` impl with rounding/fee semantics) will cause the pool account's real balance increase to be smaller than `amount`, while:
- `bonded_pool.points` is increased as if the full `amount` arrived,
- `TotalValueLocked` is increased by the full `amount`,
- `PoolMembers::points` for the depositor/joiner reflects the full nominal points.

This is exactly the audited defect pattern: "the input amount is stored/accounted instead of the amount actually settled after any deduction," which permanently inflates the points-to-balance ratio backing every other member's claim, diluting all other pool members and/or allowing the caller to redeem more value than was actually contributed.

### Impact Explanation
This falls squarely within the "Balances, assets, NFTs, staking, pools ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot. If exploited (or triggered by any non-standard `Currency`/`StakeAdapter` configuration that isn't a 1:1, fee-free transfer), it creates unbacked points in a live staking pool: total claimable value (points) exceeds the pool's actual bonded balance. This is a silent, protocol-level insolvency in the pool's internal accounting that affects all members' payouts and withdrawals, not just the depositor who triggered it — a durable value-conservation violation rather than a cosmetic bug.

### Likelihood Explanation
Likelihood is conditional: with the default `pallet-balances`/vanilla `fungible` implementation and `TransferStake`/`DelegateStake` adapters as shipped, `Currency::transfer` and `Staking::bond`/`bond_extra` move exactly the nominal amount, so the mismatch does not manifest today. The vulnerability is latent in the code path itself — no assertion or read-back verifies that the pool account's balance/stake actually increased by `amount` before points/`TotalValueLocked` are committed. Any parachain team that swaps in a different `T::Currency`/`StakeAdapter` (fee-bearing asset, custom fungible wrapper, or a future asset-based staking configuration) inherits this defect for free, with no local guard preventing it.

### Recommendation
Verify the actual balance/stake delta on the pool account (or on the destination as reported by `pledge_bond`) rather than trusting the nominal `amount`, and issue points/increment `TotalValueLocked` based on that measured value — mirroring the original fix of "store `stakedAmount`, not the raw input, in `stake.amount`." Concretely, have `StakeStrategy::pledge_bond` return the actually-bonded amount, and have `try_bond_funds` use that return value for `self.issue(...)` and the `TotalValueLocked` update, instead of computing points from `amount` prior to the transfer taking place.

### Proof of Concept
1. Configure a nomination-pools runtime instance where `Config::Currency` (or the `StakeAdapter`'s underlying asset) is a fungible implementation that delivers less than the requested amount on `transfer` (e.g., a fee-on-transfer wrapper, common for cross-chain/asset-hub style assets).
2. Call `Pools::join(origin, amount, pool_id)`.
3. Observe: `bonded_pool.points`/`PoolMembers::points` and `TotalValueLocked` are incremented by the full nominal `amount` (per `try_bond_funds` at `substrate/frame/nomination-pools/src/lib.rs:1323`), while the pool's bonded account balance only increased by `amount - fee` after `pledge_bond`'s `T::Currency::transfer` call (`substrate/frame/nomination-pools/src/adapter.rs:299`/`304`).
4. Result: the pool's points:balance ratio is now inflated relative to real backing assets — subsequent `unbond`/`withdraw_unbonded` calls by any member will pay out based on an over-stated ratio, eventually leaving later members unable to redeem their fair share (fund lock/misallocation).

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1080-1085)
```rust
	/// Issue points to [`Self`] for `new_funds`.
	fn issue(&mut self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let points_to_issue = self.balance_to_point(new_funds);
		self.points = self.points.saturating_add(points_to_issue);
		points_to_issue
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1315-1337)
```rust
	fn try_bond_funds(
		&mut self,
		who: &T::AccountId,
		amount: BalanceOf<T>,
		ty: BondType,
	) -> Result<BalanceOf<T>, DispatchError> {
		// We must calculate the points issued *before* we bond who's funds, else points:balance
		// ratio will be wrong.
		let points_issued = self.issue(amount);

		T::StakeAdapter::pledge_bond(
			Member::from(who.clone()),
			Pool::from(self.bonded_account()),
			&self.reward_account(),
			amount,
			ty,
		)?;
		TotalValueLocked::<T>::mutate(|tvl| {
			tvl.saturating_accrue(amount);
		});

		Ok(points_issued)
	}
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L289-308)
```rust
	fn pledge_bond(
		who: Member<T::AccountId>,
		pool_account: Pool<Self::AccountId>,
		reward_account: &Self::AccountId,
		amount: BalanceOf<T>,
		bond_type: BondType,
	) -> DispatchResult {
		match bond_type {
			BondType::Create => {
				// first bond
				T::Currency::transfer(&who.0, &pool_account.0, amount, Preservation::Expendable)?;
				Staking::bond(&pool_account.0, amount, &reward_account)
			},
			BondType::Extra => {
				// additional bond
				T::Currency::transfer(&who.0, &pool_account.0, amount, Preservation::Preserve)?;
				Staking::bond_extra(&pool_account.0, amount)
			},
		}
	}
```
