## Title
Unprivileged contributor can permanently block crowdloan refunds for all other contributors via unguarded `?` inside a per-recipient payout loop — (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
The external report describes a class of bug where a single malicious recipient can force a revert during a batched payout, blocking settlement for every other legitimate recipient in the same batch. The direct local analog is `pallet_crowdloan`'s `refund` extrinsic, which iterates over multiple contributors and performs a per-contributor native-currency `transfer` guarded only by the `?` operator inside the loop, with no isolation/try-catch around individual failures.

### Finding Description
`refund()` iterates the child-trie of contributions and, for each contributor, transfers their share back and only then removes their contribution record: [1](#0-0) 

```
let mut refund_count = 0u32;
let contributions = Self::contribution_iterator(fund.fund_index);
let mut all_refunded = true;
for (who, (balance, _)) in contributions {
    if refund_count >= T::RemoveKeysLimit::get() {
        all_refunded = false;
        break;
    }
    CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
    CurrencyOf::<T>::reactivate(balance);
    Self::contribution_kill(fund.fund_index, &who);
    fund.raised = fund.raised.saturating_sub(balance);
    refund_count += 1;
}
``` [2](#0-1) 

The `?` propagates any single contributor's transfer failure as the dispatchable's return value. Every FRAME extrinsic executes inside an implicit storage transaction: if the call returns `Err`, all storage mutations performed during that call — including the balance transfers and `contribution_kill` calls already executed for *other, unrelated* contributors earlier in the same loop iteration — are rolled back. This is the exact structural analog of the reported ERC777/callback issue: one uncooperative recipient in a shared payout batch can force the whole batch to revert, denying payout to everyone else in that call, not just themselves.

Because `Self::contribution_iterator` walks the child trie in a fixed (key-hash) order, the same contributor will be hit first on every retry of `refund()`, so if that contributor's transfer is made to deterministically fail, the extrinsic will fail identically on every subsequent call — permanently blocking refunds for the entire crowdloan, locking every other contributor's funds in the fund's pot account indefinitely (no governance/admin bypass exists for `refund`; `dissolve` also requires `fund.raised == 0`, which can never be reached).

This is unlike the more defensive pattern used elsewhere in the codebase for the same class of "one payout among many might fail" problem, e.g. `pallet-nomination-pools`'s migration, which explicitly catches the transfer error, logs a warning, and continues processing the remaining members instead of aborting the whole batch: [3](#0-2) 

### Impact Explanation
An unprivileged crowdloan contributor can permanently DoS the refund path for an entire crowdloan, locking every other contributor's DOT/KSM inside the fund's pot account with no way to recover it via `refund`, `withdraw` (which is unaffected but only refunds one account at a time and does not update `fund.raised`, so `dissolve` can still never succeed while `refund` cannot make progress for the deterministic first-order contributor), or governance-free path. This is a permanent user-fund lock triggered purely by an unprivileged participant's own account state, matching the "permanent user-fund lock" and "duplicate settlement/payout" impact classes in scope.

### Likelihood Explanation
Triggering the transfer failure requires no validator, collator, relayer, or admin — only a normal signed account that (a) contributes to the crowdloan, and (b) arranges for its own balance, at refund time, to make `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)` fail deterministically (e.g., by transferring funds into itself so that its free balance is within `balance` of the `Balance` type's maximum, causing the deposit-side addition to overflow and return `ArithmeticError::Overflow`, or by any other reachable transfer failure for that particular currency backend/adapter configured as `CurrencyOf<T>`). Since `refund()` is callable by any signed account (`ensure_signed(origin)?` with no restriction on which contributor), and the failing entry will be revisited first on every call due to deterministic child-trie iteration order, the DoS is self-reinforcing and requires only ordinary token transfers the attacker fully controls.

### Recommendation
In `refund()`, do not propagate individual transfer errors with `?`. Instead, catch the error per-contributor (as done in the nomination-pools migration path), skip/park the failing contributor (e.g., move them to a separate "manual retry" queue or simply `continue` while logging/emitting an event), and proceed to refund the remaining contributors in the batch. Only fail the whole extrinsic for definitely-fatal, non-recipient-controlled errors (e.g., storage corruption), never for a single recipient's balance-transfer failure.

### Proof of Concept
1. Attacker (`A`) creates a crowdloan contribution as any normal signed account via `Crowdloan::contribute`.
2. Before `refund()` is called (post crowdloan-end), attacker inflates their own account's free balance (via ordinary transfers from other attacker-controlled accounts) to sit within `contribution_balance` of `Balance::MAX`.
3. When anyone calls `Crowdloan::refund(origin, index)`, the iterator reaches `A`'s entry (deterministic child-trie order) and calls `CurrencyOf::<T>::transfer(&fund_account, &A, balance, AllowDeath)`, which returns `Err(ArithmeticError::Overflow)` because crediting `A` would overflow the `Balance` type.
4. The `?` propagates this error out of `refund()`; FRAME's storage transaction rolls back all mutations from this call, including any successful transfers/`contribution_kill`s for other contributors processed earlier in the same loop.
5. Every subsequent call to `refund()` hits the same deterministic ordering and fails identically at `A`'s entry, so no contributor is ever refunded and `fund.raised` never reaches zero, permanently blocking both `refund` and `dissolve` for the whole crowdloan. [4](#0-3)

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L502-550)
```rust
		/// Automatically refund contributors of an ended crowdloan.
		/// Due to weight restrictions, this function may need to be called multiple
		/// times to fully refund all users. We will refund `RemoveKeysLimit` users at a time.
		///
		/// Origin must be signed, but can come from anyone.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::refund(T::RemoveKeysLimit::get()))]
		pub fn refund(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			let mut fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let now = frame_system::Pallet::<T>::block_number();
			let fund_account = Self::fund_account_id(fund.fund_index);
			Self::ensure_crowdloan_ended(now, &fund_account, &fund)?;

			let mut refund_count = 0u32;
			// Try killing the crowdloan child trie
			let contributions = Self::contribution_iterator(fund.fund_index);
			// Assume everyone will be refunded.
			let mut all_refunded = true;
			for (who, (balance, _)) in contributions {
				if refund_count >= T::RemoveKeysLimit::get() {
					// Not everyone was able to be refunded this time around.
					all_refunded = false;
					break;
				}
				CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
				CurrencyOf::<T>::reactivate(balance);
				Self::contribution_kill(fund.fund_index, &who);
				fund.raised = fund.raised.saturating_sub(balance);
				refund_count += 1;
			}

			// Save the changes.
			Funds::<T>::insert(index, &fund);

			if all_refunded {
				Self::deposit_event(Event::<T>::AllRefunded { para_id: index });
				// Refund for unused refund count.
				Ok(Some(T::WeightInfo::refund(refund_count)).into())
			} else {
				Self::deposit_event(Event::<T>::PartiallyRefunded { para_id: index });
				// No weight to refund since we did not finish the loop.
				Ok(().into())
			}
		}
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L1011-1023)
```rust
						.for_each(|(who, last_claim)| {
							let outcome = T::Currency::transfer(
								&reward_account,
								&who,
								last_claim,
								Preservation::Preserve,
							);

							if let Err(reason) = outcome {
								log!(warn, "last reward claim failed due to {:?}", reason,);
							} else {
								sum_paid_out = sum_paid_out.saturating_add(last_claim);
							}
```
