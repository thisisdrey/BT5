Confirmed: `substrate/frame/balances::Currency::transfer` returns `ArithmeticError::Overflow` (verified by the test `transferring_too_high_value_should_not_panic` at [1](#0-0) ) when the destination's resulting free balance would overflow `Balance::MAX`. This gives a concrete, permissionless mechanism analogous to a "blacklisted bidder" that can permanently corrupt a batch-settlement loop in `pallet_crowdloan`.

### Title
Griefed crowdloan refund permanently locks all contributors' and the depositor's funds via an unrecoverable transfer failure inside the batched `refund()` loop - (File: `polkadot/runtime/common/src/crowdloan/mod.rs`)

### Summary
`pallet_crowdloan::refund()` iterates over a fixed number (`RemoveKeysLimit`) of contributors per call and uses `CurrencyOf::<T>::transfer(&fund_account, &who, balance, AllowDeath)?` inside the loop. Just like the Axis Finance `settle()` bug where a single blacklisted `pfBidder` broke the whole settlement, here a single contributor whose refund transfer errors (e.g. due to `ArithmeticError::Overflow` on their destination balance) causes the entire `refund()` extrinsic to fail. Because FRAME wraps each dispatchable in a storage transaction, any `Err` return unwinds **all** storage mutations made during that call — including the transfers, `contribution_kill`, and `fund.raised` decrements that had already succeeded for other contributors processed earlier in the same iteration.

### Finding Description
The vulnerable loop:
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

The child-trie contribution iterator returns entries in a deterministic order (raw `AccountId` byte order via the `Identity` hasher) [3](#0-2) , so the position of any given contributor within the iteration is fixed and reproducible across calls.

`Currency::transfer` in `pallet_balances` is not infallible: if the recipient's resulting free balance would overflow the `Balance` type, it returns `ArithmeticError::Overflow` rather than saturating, as proven by the existing unit test `transferring_too_high_value_should_not_panic` [1](#0-0) . An unprivileged attacker can:
1. Accumulate a free balance close to `Balance::MAX` on some account `A` (or use an account that already naturally has such a balance, e.g. concentrated holdings on a low-decimal test/parachain asset, or repeatedly `deposit`/`transfer` into it up to the practical maximum the chain's issuance allows).
2. Contribute a small, non-zero amount to the crowdloan with account `A` via the public, permissionless `contribute` / `contribute_all` extrinsics [4](#0-3) .
3. Wait for the crowdloan to end and call (or let anyone call) `refund()`.

Because `A`'s position in the deterministic iteration order is fixed, `refund()` will always hit `A`'s entry at the same point in every retry. Every time the loop reaches `A`, `CurrencyOf::transfer` returns `Err(ArithmeticError::Overflow)`, the `?` propagates, and the entire extrinsic aborts — rolling back **every refund already processed in that call**, including any contributors iterated before `A`. `fund.raised` never reaches zero, so:
- `refund()` can never complete for any contributor whose position in the trie iteration falls at or after `A`'s (and in the worst case, no contributor after `A` in the ordering is ever paid, since the same window is retried every call and always fails at the same point).
- `dissolve()` requires `fund.raised.is_zero()` [5](#0-4) , so the fund can never be dissolved, and the depositor's `SubmissionDeposit` (and, on `ah-ops`, lease deposits) remains permanently locked.
- The pot account's balance (all other contributors' funds) is permanently stranded, mirroring exactly the "seller can't get prefunding back / bidders can't get refunds" pattern from the source report.

This is the same broken invariant as the referenced Sherlock M-2 finding: a single participant's payout/refund failure — reachable without any malicious peer, validator, collator, or governance actor — breaks atomic batch settlement for every other participant.

### Impact Explanation
This falls squarely within the "permanent user-fund lock" and "duplicate settlement or payout" impact categories of the gate: funds belonging to every remaining crowdloan contributor plus the depositor's deposit become permanently unrecoverable on-chain, with no governance-free recovery path (governance could intervene via root/force calls, but the base protocol logic itself is broken and funds are locked indefinitely absent privileged intervention).

### Likelihood Explanation
Triggering requires only: (a) permissionless `contribute`, and (b) accumulating a large balance on one attacker-controlled account, both of which are ordinary user actions with no special privileges. The deterministic trie-iteration order guarantees the same failure point on every retry, so the attack is reliable and requires only patience/capital to acquire a balance near the numeric maximum (easier on chains/assets with smaller `Balance` types or higher unit-value tokens where extreme balances are attainable with modest capital).

### Recommendation
Do not propagate `?` on the per-contributor transfer inside the `refund` loop. Instead, treat an individual transfer failure as a skip-and-continue case (recording it for manual/governance remediation, similar to how `pallet_nomination_pools`'s migration code logs and continues on a failed reward transfer [6](#0-5) ), so that one poisoned entry cannot block or roll back refunds for all other contributors. Consider using `Preservation`/`Precision::BestEffort` semantics or an explicit try/catch-and-log pattern, and ensure `dissolve()` has an escape hatch that does not strictly require `fund.raised == 0` when individual entries are permanently unrefundable.

### Proof of Concept
1. Deploy/launch a crowdloan via `Crowdloan::create`.
2. From account `A`, first pump `A`'s free balance up near `Balance::MAX` (e.g. via repeated transfers/mints if testing on a permissioned testnet, or naturally on a chain with a very large existing balance).
3. `Crowdloan::contribute(A, para, small_amount, None)` — a small, valid, permissionless contribution.
4. Have several other accounts contribute normally as well.
5. Let the crowdloan end (`fund.end` reached or lease period passed) without winning.
6. Call `Crowdloan::refund(anyone, para)`.
7. Observe that once the iterator reaches `A`, `CurrencyOf::<T>::transfer(&fund_account, &A, balance, AllowDeath)` returns `Err(ArithmeticError::Overflow)` (as in `transferring_too_high_value_should_not_panic`), the whole extrinsic fails, and — due to FRAME's transactional dispatch rollback — even the refunds for contributors processed earlier in the same call are reverted.
8. Repeated calls to `refund()` always fail at the same point; `fund.raised` never reaches zero; `dissolve()` permanently reverts with `NotReadyToDissolve` [5](#0-4) ; all remaining contributors' funds and the depositor's deposit are permanently locked in the fund's pot account.

### Citations

**File:** substrate/frame/balances/src/tests/currency_tests.rs (L687-700)
```rust
#[test]
fn transferring_too_high_value_should_not_panic() {
	ExtBuilder::default().build_and_execute_with(|| {
		Balances::make_free_balance_be(&1, u64::MAX);
		Balances::make_free_balance_be(&2, 1);

		assert_err!(
			<Balances as Currency<_>>::transfer(&1, &2, u64::MAX, AllowDeath),
			ArithmeticError::Overflow,
		);

		assert_eq!(Balances::free_balance(1), u64::MAX);
		assert_eq!(Balances::free_balance(2), 1);
	});
```

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

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L562-566)
```rust
			// Only allow dissolution when the raised funds goes to zero,
			// and the caller is the fund creator or we are past the end date.
			let permitted = who == fund.depositor || now >= fund.end;
			let can_dissolve = permitted && fund.raised.is_zero();
			ensure!(can_dissolve, Error::<T>::NotReadyToDissolve);
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L655-668)
```rust
		/// Contribute your entire balance to a crowd sale. This will transfer the entire balance of
		/// a user over to fund a parachain slot. It will be withdrawable when the crowdloan has
		/// ended and the funds are unused.
		#[pallet::call_index(8)]
		#[pallet::weight(T::WeightInfo::contribute())]
		pub fn contribute_all(
			origin: OriginFor<T>,
			#[pallet::compact] index: ParaId,
			signature: Option<MultiSignature>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let value = CurrencyOf::<T>::free_balance(&who);
			Self::do_contribute(who, index, value, signature, AllowDeath)
		}
```

**File:** polkadot/runtime/common/src/crowdloan/mod.rs (L712-719)
```rust
	pub fn contribution_iterator(
		index: FundIndex,
	) -> ChildTriePrefixIterator<(T::AccountId, (BalanceOf<T>, Vec<u8>))> {
		ChildTriePrefixIterator::<_>::with_prefix_over_key::<Identity>(
			&Self::id_from_index(index),
			&[],
		)
	}
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L1011-1024)
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
