### Title
Duplicate/incorrect payout computation on retried bounty payouts via destructive read in `calculate_payout()` - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

### Summary
`pallet-multi-asset-bounties` exposes a multi-step, retryable payout state machine (`award_bounty` → `PayoutAttempted` → `check_status` → `retry_payment`) where the amount to pay out is (re)computed by `calculate_payout()` every time the payout flow is entered or retried. The repository's own `prdoc/stable2603-1/pr_11425.prdoc` documents that this function historically read `ChildBountiesValuePerParent` with the destructive `take()` accessor instead of a non-destructive `get()`, which deletes the storage entry on the first read. This is the exact bug class from the external report: two code paths (`do_process_payout_payment` on `award_bounty`/`retry_payment`, and `do_check_payout_payment_status` on `check_status`) share mutable settlement state and are not idempotent against being invoked more than once for the same bounty, so a second invocation on the success/retry path computes and emits an incorrect payout value.

### Finding Description
The payout pipeline is:
- `award_bounty()` calls `do_process_payout_payment()`, which calls `Self::calculate_payout(parent_bounty_id, child_bounty_id, value)` before initiating the `Paymaster::pay()` transfer [1](#0-0) .
- On failure, status becomes `PayoutAttempted { payment_status: Failed }`, and the permissionless `check_status()` extrinsic can be called repeatedly by anyone (`ensure_signed` only) to poll `do_check_payout_payment_status()`, which also calls `Self::calculate_payout(...)` before emitting `BountyPayoutProcessed` and finalizing removal [2](#0-1) .
- `retry_payment` (documented in the same status-machine comment block) can re-enter `do_process_payout_payment` again for a `Failed`/`Pending` state [3](#0-2) .

Because `calculate_payout()` derived its result from `ChildBountiesValuePerParent::<T, I>::take(...)` (per the fix description), the *first* call consumes/zeroes the shared per-parent child value, and the *second* call (from a different, independently-authorized entry point — award/retry vs. check_status success path) recomputes the payout using already-zeroed state, producing an incorrect value that is then emitted via `Event::BountyPayoutProcessed` and can diverge from the amount actually transferred by the paymaster [4](#0-3) . This mirrors the QuestBoard flaw precisely: two overlapping "closing"/settlement code paths (`closeQuestPeriod` vs `closePartOfQuestPeriod`, here `award/retry_payment` vs `check_status`) each perform amount computation and state mutation without checking whether the other path already consumed/finalized the same state, leading to a second, incorrect settlement value being recorded.

The pre-existing guards (`ensure!(payment_status.is_pending_or_failed(), ...)` in `do_process_payout_payment`, and status enum transitions) only gate *which payment-state* an entry function may act on — they do not protect the shared `ChildBountiesValuePerParent` value from being destructively read twice across the two independent call paths that both compute the payout amount.

### Impact Explanation
An incorrect payout amount emitted/transferred breaks the "settle exactly once, for the right amount" invariant required for treasury/bounty payouts. Depending on the exact zeroed-value arithmetic, this can result in beneficiaries receiving less than owed (fund lock for the rightful curator/beneficiary) or, more critically, incorrect accounting between `ChildBountiesValuePerParent` and the actual transferred amount, corrupting the bookkeeping used by parent-bounty cleanup logic across multiple child bounties. This is unbacked/incorrect settlement of value out of a pallet-controlled account — matching the "duplicate settlement or payout" and "conserve value / settle exactly once" impact criteria.

### Likelihood Explanation
Medium: `check_status` is a public, permissionless (`ensure_signed`) extrinsic that can be called any number of times whenever a bounty is in `PayoutAttempted`/`FundingAttempted`/`RefundAttempted` state, including immediately after a `Paymaster` payment reports `Failure` (which is a normal, non-malicious occurrence, e.g. transient paymaster failure). Any user can then race `retry_payment`/`award_bounty` against `check_status` polling, both of which independently call `calculate_payout()`. No special privileges, malicious relayer, or governance action is required — only ordinary use of two exposed, authorized extrinsics against the same bounty.

### Recommendation
- Ensure `calculate_payout()` never destructively mutates shared state (`ChildBountiesValuePerParent`) as a side effect of a read; use `get()` and centralize the storage cleanup exclusively in `remove_bounty()`, as already indicated by the fix description in `pr_11425.prdoc`.
- Audit all call paths that can independently trigger the same close/payout computation (`award_bounty`, `retry_payment`, `check_status`) to guarantee idempotency: computing the same amount for the same bounty state regardless of how many times it is invoked, and only advancing/removing storage once, atomically, after the terminal success is confirmed.
- Add regression tests that call the two overlapping paths (e.g., `retry_payment` then `check_status`, or vice versa) back-to-back on the same bounty and assert the emitted payout amount and final storage state are unaffected by ordering.

### Proof of Concept
1. Create and fund a child bounty under a parent bounty; award it to a beneficiary via `award_bounty`, moving it to `PayoutAttempted` with a payment id.
2. Cause the underlying `Paymaster::pay` to report `PaymentStatus::Failure` for that payment id (a normal transient failure condition).
3. Call `check_status` — internally this invokes `do_check_payout_payment_status`, which (pre-fix) calls `calculate_payout()` and destructively `take()`s `ChildBountiesValuePerParent`, then transitions status back to `PayoutAttempted{ Failed }`.
4. Call `retry_payment` (or otherwise re-enter `do_process_payout_payment`) for the same bounty — it calls `calculate_payout()` a second time, but the per-parent child value has already been zeroed by step 3, so the computed payout amount is now wrong (based on stale/zeroed state), while `Event::Paid`/`Event::BountyPayoutProcessed` are emitted again reflecting this incorrect value.
5. Compare the total value paid/emitted against the bounty's original funded value — it will not match, demonstrating a duplicate/incorrect settlement analogous to the QuestBoard `closeQuestPeriod`/`closePartOfQuestPeriod` double-processing bug. [5](#0-4) [6](#0-5)

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1147-1171)
```rust
		/// Check and update the payment status of a child-/bounty.
		///
		/// ## Dispatch Origin
		///
		/// Must be signed.
		///
		/// ## Details
		///
		/// - If the child-/bounty status is `FundingAttempted`, it checks if the funding payment
		///   has succeeded. If successful, the bounty status becomes `Funded`.
		/// - If the child-/bounty status is `RefundAttempted`, it checks if the refund payment has
		///   succeeded. If successful, the child-/bounty is removed from storage.
		/// - If the child-/bounty status is `PayoutAttempted`, it checks if the payout payment has
		///   succeeded. If successful, the child-/bounty is removed from storage.
		///
		/// ### Parameters
		/// - `parent_bounty_id`: Index of parent bounty.
		/// - `child_bounty_id`: Index of child-bounty.
		///
		/// ## Events
		///
		/// Emits [`Event::BountyBecameActive`] if the child/bounty status transitions to `Active`.
		/// Emits [`Event::BountyRefundProcessed`] if the refund payment has succeed.
		/// Emits [`Event::BountyPayoutProcessed`] if the payout payment has succeed.
		/// Emits [`Event::PaymentFailed`] if the funding, refund our payment payment has failed.
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1256-1293)
```rust
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
					let new_payment_status = Self::do_check_payout_payment_status(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						beneficiary.clone(),
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
							{
								// Drop the curator deposit when both payments succeed
								// If the child curator is the parent curator, the
								// deposit is 0
								T::Consideration::drop(curator_deposit, curator)?;
							}
							// payout succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::PayoutAttempted {
							curator: curator.clone(),
							beneficiary: beneficiary.clone(),
							payment_status: new_payment_status.clone(),
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_payout();

					(new_status, weight)
				},
				_ => return Err(Error::<T, I>::UnexpectedStatus.into()),
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1824-1846)
```rust
	fn do_process_payout_payment(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		asset_kind: T::AssetKind,
		value: T::Balance,
		beneficiary: T::Beneficiary,
		payment_status: Option<PaymentState<PaymentIdOf<T, I>>>,
	) -> Result<PaymentState<PaymentIdOf<T, I>>, DispatchError> {
		if let Some(payment_status) = payment_status {
			ensure!(payment_status.is_pending_or_failed(), Error::<T, I>::UnexpectedStatus);
		}

		let payout = Self::calculate_payout(parent_bounty_id, child_bounty_id, value);

		let source = match child_bounty_id {
			None => Self::bounty_account(parent_bounty_id, asset_kind.clone())?,
			Some(child_bounty_id) => {
				Self::child_bounty_account(parent_bounty_id, child_bounty_id, asset_kind.clone())?
			},
		};

		let id = <T as Config<I>>::Paymaster::pay(&source, &beneficiary, asset_kind, payout)
			.map_err(|_| Error::<T, I>::PayoutError)?;
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1859-1897)
```rust
	fn do_check_payout_payment_status(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		asset_kind: T::AssetKind,
		value: T::Balance,
		beneficiary: T::Beneficiary,
		payment_status: PaymentState<PaymentIdOf<T, I>>,
	) -> Result<PaymentState<PaymentIdOf<T, I>>, DispatchError> {
		let payment_id = payment_status.get_attempt_id().ok_or(Error::<T, I>::UnexpectedStatus)?;

		match <T as pallet::Config<I>>::Paymaster::check_payment(payment_id) {
			PaymentStatus::Success => {
				let payout = Self::calculate_payout(parent_bounty_id, child_bounty_id, value);

				Self::deposit_event(Event::<T, I>::BountyPayoutProcessed {
					index: parent_bounty_id,
					child_index: child_bounty_id,
					asset_kind: asset_kind.clone(),
					value: payout,
					beneficiary,
				});

				Ok(PaymentState::Succeeded)
			},
			PaymentStatus::InProgress | PaymentStatus::Unknown =>
			// nothing new to report
			{
				Err(Error::<T, I>::PayoutInconclusive.into())
			},
			PaymentStatus::Failure => {
				// assume payment has failed, allow user to retry
				Self::deposit_event(Event::<T, I>::PaymentFailed {
					index: parent_bounty_id,
					child_index: child_bounty_id,
					payment_id,
				});
				Ok(PaymentState::Failed)
			},
		}
```

**File:** prdoc/stable2603-1/pr_11425.prdoc (L1-12)
```text
title: 'fix(pallet-multi-asset-bounties): use non-destructive read in calculate_payout()'
doc:
- audience: Runtime Dev
  description: |
    Fix `calculate_payout()` using `ChildBountiesValuePerParent::take()` instead of `get()`.
    The destructive `take()` deletes the storage entry on first call, causing
    `BountyPayoutProcessed` to emit an incorrect payout value when `check_status()` calls
    `calculate_payout()` a second time on the success path. Replaced `take()` with `get()`
    and moved storage cleanup to `remove_bounty()`.
crates:
- name: pallet-multi-asset-bounties
  bump: patch
```
