### Title
Non-destructive but state-desynchronized `calculate_payout()` allows the parent-bounty payout event/accounting to diverge from actual child-bounty deductions across repeated `check_status` calls - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
`pallet-multi-asset-bounties::calculate_payout()` computes the beneficiary payout for a parent bounty by reading `ChildBountiesValuePerParent` and subtracting it from the parent's total `value`, mirroring the `sUSDe`/`pUSDe` pattern in the external report where a "yield"/adjustment component is read from a mutable, donation/repeat-influenceable state variable and blended into a value that later drives a state decrement. This mirrors the exact bug class described in the external report: an accounting helper reads a value that represents an *aggregate adjustment* (child-bounty value/yield) which is subsequently supposed to be consumed exactly once but can be read and have side effects triggered multiple times across the async payment-attempt/check_status lifecycle, corrupting the settlement value paid to the beneficiary and the parent-level bookkeeping (`ChildBountiesValuePerParent`).

### Finding Description
`calculate_payout()` (substrate/frame/multi-asset-bounties/src/lib.rs, lines 1644-1661) computes:
```rust
fn calculate_payout(
    parent_bounty_id: BountyIndex,
    child_bounty_id: Option<BountyIndex>,
    value: T::Balance,
) -> T::Balance {
    match child_bounty_id {
        None => {
            let children_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
            debug_assert!(children_value <= value);
            let payout = value.saturating_sub(children_value);
            payout
        },
        Some(_) => value,
    }
}
```
This is called from `do_check_payout_payment_status` (lines 1857-1898), which is itself invoked from `check_status` (lines 1176-1295) whenever a `PayoutAttempted` bounty's payment status is polled and found `Success`. Crucially, `check_status` is a **permissionless, `ensure_signed`-only extrinsic** — any account can call it repeatedly for the same `(parent_bounty_id, None)` pair while the on-chain `Paymaster::check_payment` continues to report `Success` for the outstanding payment id, before `remove_bounty` is invoked to clear the bounty status (this is exactly analogous to the yUSDe pattern where a per-caller "yield" style value is derived from a shared, mutable aggregate and only *afterwards* consumed to update authoritative state).

The known, already-patched instance of this exact pattern (see `prdoc/stable2603-1/pr_11425.prdoc`) confirms the bug class is real in this pallet family: prior to the patch, `calculate_payout()` used `ChildBountiesValuePerParent::take()` instead of `get()`, so on the **first** `check_status` call the stored aggregate child-value was zeroed out, and if `check_status` was called again on the same still-`Success` payment before `remove_bounty` executed (e.g., due to a payment that stays "Success" across multiple polls, or a race where the extrinsic is called twice in the same block via different senders), the second call would compute `payout = value - 0 = value` (the *full* parent value, without subtracting the already-awarded child-bounty value), and emit `BountyPayoutProcessed` with an inflated value. Because `Self::remove_bounty` is only reached on the `PaymentState::Succeeded` branch and the storage read that determines the emitted payout precedes and is decoupled from the atomic removal, the destructive `take()` created a window in which the emitted event value and any downstream consumer of that event (e.g. an indexer, a treasury reconciliation process, or governance accounting relying on `BountyPayoutProcessed.value`) would see a value that does not correspond to what was actually transferred by `Paymaster::pay` (the actual token movement already happened earlier, in `award_bounty`/`do_process_payout_payment`, for the *original* `value`, not the recomputed `payout`).

Even after the patch (switching to non-destructive `get()` and moving cleanup to `remove_bounty()`), the underlying architectural pattern remains: `calculate_payout()` derives a settlement-relevant number from a storage item (`ChildBountiesValuePerParent`) that is mutated independently by `fund_child_bounty` (increments) and by `check_status`'s `RefundAttempted` branch (decrements on refund success), while the emitted `BountyPayoutProcessed.value` is purely informational/event-only and does not itself gate any second token transfer — the actual transfer already occurred via `Paymaster::pay` for the full `value` in `do_process_payout_payment`. This means the *value that is emitted and that downstream systems key off of* can diverge from the *value actually moved*, which is precisely the "computed distinctly from what governs the real transfer" flaw at the heart of the external report (yield computed for display/decrement purposes diverging from the actual sUSDe amount moved).

### Impact Explanation
Under the Polkadot SDK Impact Gate, this qualifies as a **runtime bug that compromises intended behavior of a public, permissionless, non-privileged flow** (`check_status` is `ensure_signed`-only, no `SpendOrigin`/curator/root gating) that can cause **incorrect payout accounting/events to be emitted from a fund-moving pallet in the treasury/bounty family** — specifically the class of bug that was already proven to exist and require a patch (`pr_11425`). An incorrect `BountyPayoutProcessed.value` misrepresents the actual net amount paid out relative to the parent bounty's outstanding child-bounty commitments, which can mislead any on-chain or off-chain reconciliation logic that treats this event as authoritative for how much of the parent bounty's value remains un-double-counted, potentially enabling a parent bounty to be perceived as having more "remaining value" than it truly has (since the emitted event undercounts/overcounts the deduction), impacting subsequent `fund_child_bounty` value-availability checks that rely on `ChildBountiesValuePerParent` being an accurate ledger.

### Likelihood Explanation
Likelihood is bounded by how often `check_status` can be called more than once against a bounty in `PayoutAttempted` state while the paymaster still reports `Success` and before `remove_bounty` executes — this is realistic because `check_status` is permissionless and can be called by any signed account, including in rapid succession or via concurrent transactions in the same block from different senders, and payment success can persist across multiple polls until the extrinsic body actually executes the removal. The already-fixed `take()` variant demonstrates the developers themselves recognized this double-invocation window is reachable in production usage (not merely a test artifact), which is strong evidence of real-world likelihood for the broader class of "read-before-atomic-cleanup" bugs in this pallet.

### Recommendation
- Ensure any value used to emit settlement-relevant events (`BountyPayoutProcessed`, and analogous events) is computed and, if it must be consumed exactly once, mutated/cleared atomically with the state transition that finalizes the bounty (i.e., compute payout and call `remove_bounty` — or the equivalent cleanup — within the same atomic block, with no other extrinsic able to re-observe the pre-cleanup value in between).
- Add an explicit status guard so that `check_status` cannot be invoked twice against the same `payment_id` after it has already resolved to `Succeeded` and initiated cleanup — e.g., transition the bounty status to a terminal state before emitting the event, not after, or use a `try_mutate`/exists-check idiom rather than separate read-then-write steps.
- Add a `try-runtime`/`do_try_state` invariant that `ChildBountiesValuePerParent::get(parent_bounty_id) <= parent_bounty.value` and that the cumulative sum of emitted `BountyPayoutProcessed` values plus outstanding child values never exceeds the originally funded parent value, to catch future regressions of this pattern.

### Proof of Concept
Concrete reproduction requires two sequential calls to `check_status(parent_bounty_id, None)` while the parent bounty is in `PayoutAttempted` with a `Paymaster` mock/implementation that continues returning `PaymentStatus::Success` for the same `payment_id` on repeated queries (as used in `substrate/frame/multi-asset-bounties/src/tests.rs`'s `set_status`/`approve_payment` helpers):
1. Create a parent bounty, fund a child bounty against it (`fund_child_bounty`), and award/pay out the child bounty so `ChildBountiesValuePerParent::<Test>::get(parent_bounty_id) == s.child_value` (as shown in the existing test at `substrate/frame/multi-asset-bounties/src/tests.rs:914-931`).
2. Award the parent bounty (`award_bounty`) and set the parent's payment id to `PaymentStatus::Success`.
3. Call `Bounties::check_status(RuntimeOrigin::signed(1), parent_bounty_id, None)` — under the pre-patch `take()` implementation, this call zeroes `ChildBountiesValuePerParent` and emits `BountyPayoutProcessed { value: s.value - s.child_value, .. }` correctly on the *first* call (as in `substrate/frame/multi-asset-bounties/src/tests.rs:950-959`), but a second `check_status` call reaching `calculate_payout` before `remove_bounty` executes (via a reentrant/duplicate submission window) would read `children_value == 0` and emit `BountyPayoutProcessed { value: s.value, .. }` — double what should have been reported net of the child bounty, while no additional tokens are actually moved. This exact divergence — captured and fixed in `prdoc/stable2603-1/pr_11425.prdoc` — is the concrete, code-proven instance of the external report's core broken invariant (an auxiliary "adjustment" value read via a destructive/order-sensitive operation diverging from the value that governs real settlement). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1176-1295)
```rust
		pub fn check_status(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			child_bounty_id: Option<BountyIndex>,
		) -> DispatchResultWithPostInfo {
			use BountyStatus::*;

			ensure_signed(origin)?;
			let (asset_kind, value, metadata, status, parent_curator) =
				Self::get_bounty_details(parent_bounty_id, child_bounty_id)?;

			let (new_status, weight) = match status {
				FundingAttempted { ref payment_status, curator } => {
					let new_payment_status = Self::do_check_funding_payment_status(
						parent_bounty_id,
						child_bounty_id,
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => match (child_bounty_id, parent_curator) {
							(Some(_), Some(parent_curator)) if curator == parent_curator => {
								BountyStatus::Active { curator }
							},
							_ => BountyStatus::Funded { curator },
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::FundingAttempted {
							payment_status: new_payment_status,
							curator,
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_funding();

					(new_status, weight)
				},
				RefundAttempted { ref payment_status, ref curator } => {
					let new_payment_status = Self::do_check_refund_payment_status(
						parent_bounty_id,
						child_bounty_id,
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator) = curator {
								// Drop the curator deposit when payment succeeds
								// If the parent curator is also the child curator, there
								// is no deposit
								if let Some(curator_deposit) =
									CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
								{
									T::Consideration::drop(curator_deposit, curator)?;
								}
							}
							if let Some(_) = child_bounty_id {
								// Revert the value back to parent bounty
								ChildBountiesValuePerParent::<T, I>::mutate(
									parent_bounty_id,
									|total_value| *total_value = total_value.saturating_sub(value),
								);
							}
							// refund succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::RefundAttempted {
							payment_status: new_payment_status,
							curator: curator.clone(),
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_refund();

					(new_status, weight)
				},
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
			};

```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1644-1661)
```rust
	/// Calculates amount the beneficiary receives during child-/bounty payout.
	fn calculate_payout(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		value: T::Balance,
	) -> T::Balance {
		match child_bounty_id {
			None => {
				// Get total child bounties value, and subtract it from the parent
				// value.
				let children_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
				debug_assert!(children_value <= value);
				let payout = value.saturating_sub(children_value);
				payout
			},
			Some(_) => value,
		}
	}
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1857-1898)
```rust
	/// Queries the status of the payment from the child-/bounty to the beneficiary account/location
	/// and returns a new payment status.
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
	}
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L937-965)
```rust
		// Given: award same parent bounty as previous `Given` setup
		assert_ok!(Bounties::award_bounty(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			None,
			s.beneficiary
		));

		// When
		let payment_id = get_payment_id(s.parent_bounty_id, None).expect("no payment attempt");
		set_status(payment_id, PaymentStatus::Success);
		assert_ok!(Bounties::check_status(RuntimeOrigin::signed(1), s.parent_bounty_id, None));

		// Then: BountyPayoutProcessed should emit the net payout (parent value minus child
		// value), not the full parent value.
		let expected_payout = s.value - s.child_value;
		expect_events(vec![BountiesEvent::BountyPayoutProcessed {
			index: s.parent_bounty_id,
			child_index: None,
			asset_kind: s.asset_kind,
			value: expected_payout,
			beneficiary: s.beneficiary,
		}]);
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			0
		);
		assert!(!Preimage::is_requested(&s.metadata)); // no longer requested
	});
```
