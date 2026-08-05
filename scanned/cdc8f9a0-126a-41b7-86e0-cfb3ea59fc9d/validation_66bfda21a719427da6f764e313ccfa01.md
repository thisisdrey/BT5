### Title
`Crowdloan::refund` permanently deadlocks all remaining contributor refunds if any single transfer errors mid-loop - ([File: polkadot/runtime/common/src/crowdloan/mod.rs])

### Summary
`pallet-crowdloan`'s `refund` extrinsic iterates over a child-trie of contributors and pushes funds to each of them with `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;` inside the loop. Because the `?` propagates any transfer error straight out of the dispatchable, and Substrate extrinsics roll back all storage changes on error, a single contributor whose transfer fails aborts the whole call — including the previously-successful transfers made *within the same call*. Since the child-trie iteration order is deterministic, the same failing contributor is hit at the same point on every subsequent call, permanently blocking refunds for every contributor after it. This mirrors the reported `_payment()`/`_transferETH()` pattern in the external report, where a single reverting recipient locks up an entire batch payout.

### Finding Description
`refund` is a permissionless, unprivileged, non-atomic-by-design batch payout: [1](#0-0) 

```
/// Origin must be signed, but can come from anyone.
pub fn refund(origin: OriginFor<T>, #[pallet::compact] index: ParaId) -> DispatchResultWithPostInfo {
    ensure_signed(origin)?;
    ...
    for (who, (balance, _)) in contributions {
        if refund_count >= T::RemoveKeysLimit::get() { all_refunded = false; break; }
        CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?;
        CurrencyOf::<T>::reactivate(balance);
        Self::contribution_kill(fund.fund_index, &who);
        fund.raised = fund.raised.saturating_sub(balance);
        refund_count += 1;
    }
    Funds::<T>::insert(index, &fund);
    ...
}
```

The `?` on the `transfer` call causes the dispatchable to return `Err`, and by Substrate's transactional dispatch semantics **all state mutations from this extrinsic execution are rolled back**, including the `contribution_kill` removals and `fund.raised` decrements for the contributors processed earlier in the *same* loop iteration. Because `Self::contribution_iterator` walks the child trie in a fixed deterministic order, the failing entry sits at the exact same position on every future call to `refund` for that fund — so every subsequent call reprocesses the same prefix, hits the same failing transfer, and reverts again. No contributor after the poison entry can ever be refunded, and even contributors before it (which were refunded transiently during a failed call) get their state rolled back and have to be re-processed forever, without success.

This is functionally identical to the `BoxExchange._payment()` bug: a single non-cooperative/edge-case recipient in a push-payment loop causes the entire batch (and all subsequent retries of that batch) to fail permanently, because the code does not isolate individual transfer failures.

A concrete failure trigger: `pallet-balances::transfer` (via `Currency::transfer`) fails with an existential-deposit violation if the destination account does not exist and the transferred amount is below `ExistentialDeposit`. A contributor who contributed a tiny amount (crowdloan pallet does not enforce contributions ≥ ED beyond `MinContribution`, which is configurable and can be set below ED, or the account may become dust/reaped between contribution and refund for unrelated reasons) and whose account has since been reaped (e.g. spent all funds and lost its only provider reference) will cause `transfer(&fund_account, &who, balance, AllowDeath)` to fail with `Error::<T>::ExistentialDeposit` when the pallet tries to re-create the account below ED. That failure aborts the whole `refund` call.

### Impact Explanation
This is a public underpriced/unauthorized-DoS class bug in a live pallet used on relay chains / production runtimes with crowdloans: an unprivileged, unprivileged-caller-triggerable extrinsic (`refund`, callable by "anyone") can be made to permanently fail for an entire crowdloan fund, indefinitely locking every remaining contributor's refund behind the one poisoned entry. This is a "permanent user-fund lock" per the impact gate — contributors' locked contribution can never be automatically refunded via this dispatchable once the fault is hit, requiring manual/governance intervention (e.g. `force_transfer` bypass) that is outside the normal user flow, and blocking the fund from ever reaching `Dissolved` state (since `dissolve` requires `fund.raised.is_zero()`).

### Likelihood Explanation
The path requires no privileged actor, no malicious peer/validator/relayer, and no admin action — any contributor account state (e.g. becoming dust/reaped, or a tiny `MinContribution`) combined with anyone calling the permissionless `refund` extrinsic is sufficient to trigger it. It is a deterministic, reproducible logic flaw in ordinary dispatch/rollback semantics rather than a chance race condition, making it a realistic, easily reachable scenario for real crowdloan campaigns with many small contributors.

### Recommendation
Do not use `?` to propagate individual-recipient transfer failures inside the refund loop. Catch the `Result` from `CurrencyOf::<T>::transfer` per contributor, and on failure either:
- skip/leave that contributor's `contribution` entry untouched (so their principal is not force-removed) and continue refunding the rest, emitting a `RefundFailed` event for out-of-band resolution, or
- fall back to a "pull" pattern where individual contributors call a `claim_refund(index)` extrinsic to withdraw their own contribution, so a bad account only affects itself and never blocks other contributors' claims — mirroring the pull-over-push recommendation in the original report.

### Proof of Concept
1. Deploy a crowdloan fund (`Crowdloan::create`) with `MinContribution` set (or leave contributions unconstrained if governance sets it low).
2. Have several accounts contribute, including one contributor `X` who contributes an amount and later drains/loses their account so it is reaped (zero providers, zero balance) before the crowdloan ends.
3. Let the crowdloan end (`ensure_crowdloan_ended` passes).
4. Anyone calls `Crowdloan::refund(origin, index)`.
5. Iteration reaches contributor `X`; `CurrencyOf::<T>::transfer(&fund_account, &X, balance, AllowDeath)` attempts to re-create `X`'s account with `balance < ExistentialDeposit`, returning `Err(Error::ExistentialDeposit)`.
6. The `?` propagates the error, the whole extrinsic reverts (all refunds processed earlier in this call, if any, are rolled back), and `Funds::<T>::insert` never commits `contribution_kill`s.
7. Every subsequent call to `refund` for this `index` reaches the same contributor `X` at the same point in iteration and fails identically — contributions before and after `X` are永久ly stuck, and `dissolve` (which requires `fund.raised.is_zero()`) can never succeed. [1](#0-0)

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L502-536)
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
```
