### Title
Crowdloan `refund` payout loop can be permanently DoS'd by a single unrefundable contributor - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
The `send`-in-a-loop bug class from the external report — one uncooperative payee reverting the whole multi-destination payout, blocking every other legitimate payee — has a structural analog in `Crowdloan::refund`. This extrinsic iterates over a fund's contributors and calls a fallible `Currency::transfer(...)?` inside the loop body, so a single failing transfer aborts the entire dispatch and rolls back the storage changes for *every* contributor processed so far in that call, not just the failing one.

### Finding Description
`refund` is a public, unsigned-permission call (`ensure_signed(origin)?` only — anyone can call it) meant to iteratively pay back contributors of an ended/dissolved crowdloan, up to `RemoveKeysLimit` per call: [1](#0-0) 

The relevant loop body is:
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

Because `transfer(...)?` uses the early-return `?` operator inside a `DispatchResultWithPostInfo` function, any single failing transfer inside the loop propagates as an `Err` for the entire `refund` call. Substrate's transactional dispatch model means that when a dispatchable returns `Err`, all storage mutations performed during that call — including `contribution_kill` and `fund.raised` updates for contributors already successfully refunded earlier in the same loop iteration set — are rolled back. This is exactly the "one payee blocks the payout to all others" pattern from the report: the loop has no per-item try/catch, no skip-and-continue, and no removal of the offending contributor before/after the attempt.

Since the iterator (`Self::contribution_iterator`) always starts deterministically from the same trie ordering and nothing removes or skips the failing contributor on error, every subsequent call to `refund` for that fund will re-encounter the same first unrefundable contributor and fail again at the same point, permanently blocking refunds for that fund unless someone with root access intervenes (governance is out of scope per the impact gate, so this is a plain contributor-triggered condition).

### Impact Explanation
Any contributor can make their own destination address behave in a way that causes their `Currency::transfer` inside this loop to fail deterministically (e.g., an account state that rejects deposit due to consumer/provider or existence-requirement constraints under `AllowDeath`, similar in spirit to a fallback-revert in the Solidity analog). Because the whole batched loop shares one fallible transactional context, that single unrefundable contributor blocks *all* other contributors ordered after them in the child-trie iteration from ever being refunded through `refund`, for as long as the fund remains in this state. This can permanently lock user funds in the crowdloan fund account and prevent `dissolve` from ever succeeding (which requires `fund.raised.is_zero()`), stalling cleanup of the fund indefinitely — a direct fund-lock impact within the accepted impact categories.

### Likelihood Explanation
The call is unsigned-permission (any signed account) and requires no elevated privilege, matching the "unprivileged attacker" requirement. The only precondition is that the attacker be one of the fund's contributors (trivial — anyone can contribute any dust amount before the crowdloan ends) and that their account be placed into a state that makes the `transfer` fail on refund. This is a realistic, repeatable condition rather than a rare race, and does not require a malicious validator, collator, relayer, or governance actor — it is purely a state the depositor controls for their own account.

### Recommendation
Do not let a single failed transfer abort the whole batched refund loop. Options:
- Use `Currency::transfer(...)` fallibly per-item without `?`, log/skip on error, and continue to the next contributor (mirroring "favor pull payments"/isolate failures), or
- Switch refund to a pull-payment model where each contributor withdraws individually (as `withdraw` already does per-user), and deprecate/guard the batched auto-refund path, or
- Track and skip/quarantine a contributor whose transfer failed (e.g., move them to a separate "failed" set) so the loop can make forward progress for the remaining contributors even if one keeps failing.

### Proof of Concept
1. Contributor `A` (attacker) and contributors `B`, `C`, ... contribute to a crowdloan fund; fund ends without winning a slot.
2. Attacker `A` arranges their own account so any `transfer(&fund_account, &A, balance, AllowDeath)` will error (state fully controlled by `A`, no privileged actor needed).
3. Anyone calls `Crowdloan::refund(origin, index)`. The iterator reaches `A` within the current `RemoveKeysLimit` batch (guaranteed if `A` contributed early / trie ordering places them first, or simply because the batch size covers all contributors).
4. `CurrencyOf::<T>::transfer(&fund_account, &A, balance, AllowDeath)?` returns `Err`, aborting `refund`; all storage changes in this call (including `contribution_kill` for contributors already processed earlier in this same loop pass) roll back.
5. Every subsequent call to `refund` re-hits the same failure at the same point in the deterministic iterator, so `B`, `C`, etc. never get refunded and `fund.raised` never reaches zero, permanently blocking `dissolve` and locking the remaining contributors' funds in the fund account.

### Citations

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L502-537)
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
