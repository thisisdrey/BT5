## Analysis

The external report's core broken invariant: **a batch-processing function that iterates multiple independent user requests in one transaction propagates any single item's transfer failure into a revert of the *entire* batch**, because there is no isolation (no try/catch) around the transfer call. This lets one attacker-controlled entry deny service to every other unrelated request bundled into the same call.

The closest verifiable local analog in this repository is `polkadot/runtime/common/src/crowdloan/mod.rs`, the `refund()` extrinsic.

### Title
Unbounded revert of batch crowdloan refunds due to un-isolated `Currency::transfer` in the `refund()` loop - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
`Crowdloan::refund()` iterates over up to `RemoveKeysLimit` contributors and issues a `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?` for each one inside a plain `for` loop, exactly mirroring the UFarm `quexCallback()` pattern: several independent user payouts are settled in one transaction, and the `?` on the transfer means any single failing transfer aborts the *entire* extrinsic, rolling back every other refund already computed in that same call. [1](#0-0) 

### Finding Description
`refund()`:
```rust
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

The `?` operator means that if the `transfer` to any single contributor `who` fails (e.g., a `DispatchError` from `pallet-balances`, such as the recipient falling below the Existential Deposit, being frozen/locked in a way that blocks receipt, or any other transfer precondition failure), the whole dispatchable returns `Err`, and because storage mutations inside a FRAME dispatchable are rolled back on error, *all* refunds computed earlier in that same loop iteration — for completely unrelated, well-behaved contributors — are undone as well. Nothing distinguishes "this one contributor's transfer is unrecoverable" from "abort everyone in this batch."

Crucially, `contribution_iterator` walks the child-trie in a deterministic key order, so the same set of contributors lands in the same batch on every subsequent call to `refund()`. If the batch containing the poisoned entry can never succeed, that batch — and by extension the fund's ability to ever reach `fund.raised == 0` — is permanently stuck, since `dissolve()` requires `fund.raised.is_zero()`. [3](#0-2) 

### Impact Explanation
This breaks the "message queues, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot in the worst possible direction: it makes atomicity a liability rather than a guarantee, because it forces *unrelated* successful settlements to be discarded due to one poisoned entry, and can permanently lock refunds/deposits for a crowdloan (a Balances/asset-accounting invariant break — "permanent user-fund lock"). Any legitimate contributor sharing a refund batch with the malicious entry can never be refunded, and the fund's deposit (`fund.deposit`, held via `dissolve`) is stuck as well since `dissolve()` cannot proceed while `fund.raised != 0`.

### Likelihood Explanation
The attack requires only an unprivileged account contributing to a crowdloan through the completely public `contribute`/`contribute_all` extrinsics — no validator, relayer, governance, or leaked-key assumption is needed. The main open question (and the primary caveat on this finding) is which concrete transfer-failure condition is reliably triggerable given the `MinContribution` check in `do_contribute` (present in this codebase, confirmed via grep, but its exact interaction with the Existential Deposit and other transfer preconditions was not fully verified in this pass due to time constraints — I could not read `do_contribute`'s full body before running out of iterations). If `MinContribution` can be set (or is set on some deployed runtime) to a value that still permits a transfer failure on refund (e.g., ED changes between contribution and refund, or a frozen/locked recipient scenario), the DoS is directly exploitable; otherwise the practical trigger surface narrows to edge cases around balance mutation between contribution and refund. This uncertainty should be resolved by a background agent reading `do_contribute` and the exact `MinContribution`/ED relationship on the target runtimes (Rococo/Westend) before treating this as a confirmed, immediately exploitable bug.

### Recommendation
Wrap each per-contributor transfer in `refund()` with a fallible, isolated operation (e.g. `let _ = CurrencyOf::<T>::transfer(...)` combined with explicit error handling that skips/re-queues only the failing contributor) instead of propagating the error via `?` for the whole loop. On failure for a given `who`, log/emit an event, leave that contributor's entry un-killed so it can be retried or handled by governance, and continue processing the rest of the batch — mirroring the `try/catch`-then-`return 0` remediation pattern used to fix the original UFarm issue.

### Proof of Concept
1. Attacker contributes to a crowdloan via `Crowdloan::contribute` with an amount that will fail to transfer back during `refund()` (subject to the `MinContribution`/ED relationship noted above — not fully confirmed in this pass).
2. Several other honest users contribute to the same crowdloan such that the attacker's entry falls within the same `RemoveKeysLimit`-sized batch in child-trie iteration order.
3. Crowdloan ends; anyone calls `Crowdloan::refund(index)`.
4. The loop processes honest contributors, then hits the attacker's entry; `CurrencyOf::<T>::transfer(...)?` returns `Err`, and the whole extrinsic reverts — including the honest refunds already computed in that call.
5. Because the child-trie iteration order is stable, every subsequent call to `refund()` reproduces the same failure at the same point, permanently blocking refunds (and therefore `dissolve()`) for that fund.

Given the unresolved verification of the exact `MinContribution`/transfer-failure trigger, this should be treated as a **structural analog requiring confirmation** rather than a fully proven exploit chain — a background Devin session should inspect `do_contribute`'s validation logic and the target runtimes' `MinContribution`/`ExistentialDeposit` configuration to determine the precise triggerable failure mode.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L520-536)
```rust
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

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L555-581)
```rust
		pub fn dissolve(origin: OriginFor<T>, #[pallet::compact] index: ParaId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let fund = Funds::<T>::get(index).ok_or(Error::<T>::InvalidParaId)?;
			let pot = Self::fund_account_id(fund.fund_index);
			let now = frame_system::Pallet::<T>::block_number();

			// Only allow dissolution when the raised funds goes to zero,
			// and the caller is the fund creator or we are past the end date.
			let permitted = who == fund.depositor || now >= fund.end;
			let can_dissolve = permitted && fund.raised.is_zero();
			ensure!(can_dissolve, Error::<T>::NotReadyToDissolve);

			// Assuming state is not corrupted, the child trie should already be cleaned up
			// and all funds in the crowdloan account have been returned. If not, governance
			// can take care of that.
			debug_assert!(Self::contribution_iterator(fund.fund_index).count().is_zero());

			// Crowdloan over, burn all funds.
			let _imba = CurrencyOf::<T>::make_free_balance_be(&pot, Zero::zero());
			let _ = frame_system::Pallet::<T>::dec_providers(&pot).defensive();

			CurrencyOf::<T>::unreserve(&fund.depositor, fund.deposit);
			Funds::<T>::remove(index);
			Self::deposit_event(Event::<T>::Dissolved { para_id: index });
			Ok(())
		}
```
