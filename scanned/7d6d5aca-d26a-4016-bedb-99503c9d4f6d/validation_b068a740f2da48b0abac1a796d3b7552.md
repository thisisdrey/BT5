### Title
`pallet-salary::check_payment` walks back `total_unregistered_paid` and rewinds `last_active` on a reported payment failure, letting a claimant re-enter `do_payout` in the same cycle and draw twice from the per-cycle budget - (File: `substrate/frame/salary/src/lib.rs`)

### Summary
The C4 finding's core broken invariant is: a per-actor accumulator that gates a hard cap (`totalDepositedAmountPerUser` vs `depositCap`) is decremented by an amount whenever the counterpart "failed" flow is invoked, without the decrement path being bound to a verified, un-repeatable true failure. This lets an actor arrange (or exploit an ambiguous) "failure" signal to roll the accumulator back and re-enter the capped operation, exceeding the intended cap. The same accumulator/rewind pattern exists in `pallet-salary`'s cycle-budget bookkeeping.

### Finding Description
`pallet-salary` tracks a per-cycle budget via `StatusType::total_unregistered_paid` and enforces it as a spending cap for "walk-in" (unregistered) claimants in `do_payout`: [1](#0-0) 

The pot available to a walk-in claimant is `budget - total_registrations - total_unregistered_paid`, and `total_unregistered_paid` is only ever accrued when a payment attempt is made, never re-validated against the beneficiary's actual final balance.

When a claimant later calls `check_payment` to resolve the outcome of the `Paymaster::pay` call, a reported `PaymentStatus::Failure` for an unregistered attempt rewinds both the spend accumulator and the claimant's cycle marker so the claim can be retried: [2](#0-1) 

```
status.total_unregistered_paid.saturating_reduce(amount);
claimant.last_active.saturating_reduce(1u32.into());
claimant.status = ClaimState::Nothing;
```

This is exactly the C4 pattern: the cap-tracking counter (`total_unregistered_paid`, analog of `totalDepositedAmountPerUser`) is decremented on a "failed" claim, and `last_active` is rewound so the very next `do_payout` call matches the `Nothing | Attempted{..} | Registered(_) if claimant.last_active < status.cycle_index` branch again in the same cycle — i.e. the "claim failed" reset re-opens the gate that was supposed to be single-use per cycle.

The correctness of this mechanism depends entirely on `T::Paymaster::check_payment` returning `Failure` if and only if the beneficiary genuinely never received funds. For the shipped XCM-based `Pay` implementation used with this pallet (`PayOverXcm`/`TransferOverXcmHelper`), the reported status is derived from the remote chain's XCM `ExecutionResult`: [3](#0-2) 

```
Response::ExecutionResult(None) => TransferStatus::Success,
Response::ExecutionResult(Some(_)) => TransferStatus::Failure,
```

XCM message execution is not transactionally atomic across instructions: instructions preceding a failing instruction are not rolled back — their state effects (e.g. an asset deposit that already landed) persist even though the overall message reports an error. `payout_other` lets the caller choose an arbitrary `beneficiary` for the remote transfer, and the remote-side message constructed by `remote_transfer_xcm_free_execution` / `remote_transfer_xcm_paying_fees` contains more than the bare deposit instruction (origin/derivation and reporting steps). If any trailing instruction on the destination errors after the deposit instruction has already executed, the destination reports `ExecutionResult(Some(_))` (Failure) even though the beneficiary already received the funds. Calling `check_payment` at that point:
- decrements `total_unregistered_paid` by the (already-paid-out) `amount`, restoring budget headroom that was in fact already spent, and
- rewinds `last_active`, letting the caller immediately call `do_payout` again in the same cycle.

The claimant therefore both keeps the first (silently successful) payment and consumes fresh "pot" budget for a second payment in the same cycle, exceeding the intended once-per-cycle, budget-capped payout — a direct fund-accounting break, not merely a UX inconvenience.

### Impact Explanation
This is a treasury/reward-payout accounting break, matching the "Balances, assets, ... treasury spends, bridge rewards ... must conserve value and settle exactly once" pivot. An unprivileged member of the ranked collective can, in the worst case, double-spend the cycle's `Budget` by triggering a partially-failing remote XCM payment and then calling `check_payment`, without needing a malicious relayer, validator, or governance actor — the state transition is entirely internal to `pallet-salary` and is a direct consequence of trusting an external `Pay::check_payment` boolean outcome to gate reversible accounting for a value that may have already moved.

### Likelihood Explanation
Medium: it requires the underlying `Pay`/`Transfer` implementation to be able to report `Failure` for a message where the deposit instruction nonetheless already succeeded (a property of XCM's non-atomic instruction execution rather than of a compromised party), and it requires an unregistered ("walk-in") claim path, which is the less common but explicitly supported flow in `do_payout`. As with the original finding, this is a "context" class issue that only manifests under specific but realistic operational conditions (asynchronous cross-chain payment confirmation), which is why the equivalent C4 report was rated Medium rather than High/Critical.

### Recommendation
- Do not decrement `total_unregistered_paid` (or rewind `last_active`) purely on a `PaymentStatus::Failure` report; instead require the `Pay` implementation to guarantee an atomic and consistent success/failure signal for the entire beneficiary-affecting effect, or add a secondary on-chain balance/state check before restoring cap headroom.
- Alternatively, keep the spent amount "reserved" (not yet chargeable to a retried payout in the same cycle) until independently reconciled, mirroring the C4 recommendation of always accounting for the amount and only gating the *cap check*, not the underlying counter update, on ambiguous states.
- Apply the same review to `pallet-treasury::check_status`, which has an analogous `Attempted -> Failed` transition but does not touch a cap-style spend counter today; ensure any future coupling between `check_status` and a spendable-limit accumulator uses the same fix.

### Proof of Concept
1. Runtime configures `pallet-salary` with `Paymaster = PayOverXcm<...>` (or `PayAccountId32OnChainOverXcm`) and a non-zero `Budget`.
2. Alice is inducted and, in the payout window, calls `payout_other(beneficiary)` for a beneficiary location on a remote chain, taking the *unregistered* branch of `do_payout`: `total_unregistered_paid` accrues `amount`, `claimant.status = Attempted{registered: None, id, amount}`, `claimant.last_active = cycle_index`.
3. The remote XCM message executes the deposit instruction successfully (beneficiary receives funds) but a later instruction in the same message errors, so the remote chain's `QueryResponse` carries `ExecutionResult(Some(error))`.
4. `TransferOverXcmHelper::check_transfer` (`polkadot/xcm/xcm-builder/src/transfer.rs:239-250`) maps this to `TransferStatus::Failure`.
5. Alice calls `check_payment`: `pallet-salary` sees `PaymentStatus::Failure`, and because `registered == None`, executes `status.total_unregistered_paid.saturating_reduce(amount)` and `claimant.last_active.saturating_reduce(1)`, setting `claimant.status = Nothing` (`substrate/frame/salary/src/lib.rs:346-360`).
6. Alice immediately calls `payout` (or `payout_other`) again in the same cycle; `do_payout`'s unregistered branch matches again (`claimant.last_active < status.cycle_index`), computes a fresh `pot` using the now-reduced `total_unregistered_paid`, and pays Alice a second time — while she already retained the funds from step 3.

I was not able to execute this against a live/test runtime in this session (no test harness run), so the exact remote-XCM message shape that produces a post-deposit trailing failure was reasoned from `remote_transfer_xcm_free_execution`/`remote_transfer_xcm_paying_fees` construction and XCM's documented non-atomic instruction semantics rather than from a captured trace; confirming the precise instruction sequence would strengthen the PoC.

### Citations

**File:** substrate/frame/salary/src/lib.rs (L346-360)
```rust
			match T::Paymaster::check_payment(id) {
				PaymentStatus::Failure => {
					// Payment failed: we reset back to the status prior to payment.
					if let Some(amount) = registered {
						// Account registered; this makes it simple to roll back and allow retry.
						claimant.status = ClaimState::Registered(amount);
					} else {
						// Account didn't register; we set it to `Nothing` but must decrement
						// the `last_active` also to ensure a retry works.
						claimant.last_active.saturating_reduce(1u32.into());
						claimant.status = ClaimState::Nothing;
						// Since it is not registered, we must walk back our counter for what has
						// been paid.
						status.total_unregistered_paid.saturating_reduce(amount);
					}
```

**File:** substrate/frame/salary/src/lib.rs (L405-422)
```rust
				Nothing | Attempted { .. } | Registered(_)
					if claimant.last_active < status.cycle_index =>
				{
					// Not registered for this cycle (or stale registration from previous cycle).
					// Pay from whatever is left.
					let rank = T::Members::rank_of(&who).ok_or(Error::<T, I>::NotMember)?;
					let ideal_payout = T::Salary::get_salary(rank, &who);

					let pot = status
						.budget
						.saturating_sub(status.total_registrations)
						.saturating_sub(status.total_unregistered_paid);

					let payout = ideal_payout.min(pot);
					ensure!(!payout.is_zero(), Error::<T, I>::ClaimZero);

					status.total_unregistered_paid.saturating_accrue(payout);
					(payout, None)
```

**File:** polkadot/xcm/xcm-builder/src/transfer.rs (L239-250)
```rust
	fn check_transfer(id: Self::QueryId) -> TransferStatus {
		use QueryResponseStatus::*;
		match Querier::take_response(id) {
			Ready { response, .. } => match response {
				Response::ExecutionResult(None) => TransferStatus::Success,
				Response::ExecutionResult(Some(_)) => TransferStatus::Failure,
				_ => TransferStatus::Unknown,
			},
			Pending { .. } => TransferStatus::InProgress,
			NotFound | UnexpectedVersion => TransferStatus::Unknown,
		}
	}
```
