Based on my research, the strongest concrete local analog to the ERC20RebaseDistributor's "anyone can prolong distribution" bug class is in `pallet-treasury`'s `payout` dispatchable.

### Title
Permissionless `payout()` unconditionally resets `expire_at`, letting anyone indefinitely postpone treasury spend expiry - (File: `substrate/frame/treasury/src/lib.rs`)

### Summary
`Pallet::payout` is callable by any signed account (not just the beneficiary) for any pending spend index, and on every call it unconditionally pushes `spend.expire_at` forward by a full `PayoutPeriod`, exactly mirroring the reported ERC20RebaseDistributor pattern where a cheap, repeatable, permissionless call resets a time-boxed distribution/expiry window.

### Finding Description
`spend()` creates an approved spend with a bounded claim window: `expire_at = valid_from + PayoutPeriod` [1](#0-0) . The design intent, per the pallet docs, is that an approved spend "must be claimed... within one `PayoutPeriod`", otherwise it is meant to expire and be cleaned up by `check_status` [2](#0-1) .

However, `payout()` only requires `ensure_signed(origin)` — it does not check that the caller is the beneficiary or any privileged origin [3](#0-2) . Every time `payout()` successfully initiates a payment attempt (status `Pending` or `Failed`), it unconditionally does:
```
spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
``` [4](#0-3) 

This reset happens regardless of whether the underlying `T::Paymaster::pay` call will ultimately succeed or fail — it fires as soon as `pay()` returns an `Id`, before any confirmation. The only gate against re-invoking `payout()` again immediately is the status check `matches!(spend.status, PaymentState::Pending | PaymentState::Failed)` [5](#0-4) , which is cleared back to `Failed` by anyone calling the equally permissionless `check_status()` once the paymaster reports `Status::Failure` [6](#0-5) .

This is a documented, acknowledged design tension in the repo itself: PR #7959 explicitly introduced this "reset `expire_at` on every valid payout attempt" behavior to avoid penalizing a legitimate claimant during liquidity shortages [7](#0-6) , and the repo's own test `payout_extends_expiry` demonstrates the mechanic directly: a `payout()` call at block 4 fails, and the retry succeeds at block 7 — after the *original* expiry would have already passed — solely because `expire_at` was advanced by the first attempt [8](#0-7) .

Because `payout()` is open to *any* signed account for *any* spend index, an attacker who does not even control the beneficiary can repeatedly cycle `payout()` → `check_status()` → `payout()` on a spend whose paymaster route intermittently reports failure (e.g., a cross-chain `Paymaster` where destination-side congestion, filters, or an invalid/blocked beneficiary route cause `check_payment` to resolve to `Status::Failure`), and each cycle costs only ordinary transaction fees — a tiny, attacker-controlled amount of "work" analogous to the `distribute(1)` wei calls in the original report.

### Impact Explanation
This breaks the intended temporal bound on approved treasury spends: a spend that should expire and be pruned by `check_status` after `PayoutPeriod` if it is not genuinely completed can instead be kept perpetually "alive" by an unrelated, unprivileged third party, indefinitely deferring the point at which governance/treasury can consider the allocation stale and free to reconsider. This directly parallels the original finding's core broken invariant — "a public function that resets a time-bounded distribution/expiry window can be re-triggered cheaply and repeatedly by anyone, unbounded by real progress toward completion."

### Likelihood Explanation
Likelihood is moderate: it requires a `Paymaster` implementation whose `check_payment` can be made (or happens) to report `Failure` for a given spend, which is realistic for cross-chain/XCM-based paymasters subject to external network conditions and does not require any privileged actor, malicious validator/collator, or leaked keys — only ordinary signed transactions.

### Recommendation
Restrict `payout()` extension of `expire_at` to cases where the caller is the beneficiary (or an authorized proxy of it), or only extend `expire_at` after a confirmed `check_status` failure rather than unconditionally on every `pay()` call, and/or cap the number of retry-driven expiry extensions per spend so an uninvolved third party cannot indefinitely defer expiry.

### Proof of Concept
1. Governance approves a spend via `spend()` with `PayoutPeriod = N` blocks.
2. Any signed account (not necessarily the beneficiary) calls `payout(index)`; `T::Paymaster::pay` returns an id, `expire_at` is pushed to `now + N` [4](#0-3) .
3. The payment later resolves to `Status::Failure` (e.g., destination-side rejection); anyone calls `check_status(index)`, moving status back to `Failed` [9](#0-8) .
4. Repeat steps 2–3 every `< N` blocks indefinitely — as demonstrated structurally by `payout_extends_expiry` [8](#0-7)  — so the spend's `expire_at` is perpetually renewed by an unprivileged, unrelated caller and the spend never reaches the "processed/expired" cleanup path in `check_status`.

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L62-69)
```rust
//! Spends can be initiated using either the `spend_local` or `spend` dispatchable. The
//! `spend_local` dispatchable enables the creation of spends using the native currency of the
//! chain, utilizing the funds stored in the pot. These spends are automatically paid out every
//! [`pallet::Config::SpendPeriod`]. On the other hand, the `spend` dispatchable allows spending of
//! any asset kind managed by the treasury, with payment facilitated by a designated
//! [`pallet::Config::Paymaster`]. To claim these spends, the `payout` dispatchable should be called
//! within some temporal bounds, starting from the moment they become valid and within one
//! [`pallet::Config::PayoutPeriod`].
```

**File:** substrate/frame/treasury/src/lib.rs (L661-664)
```rust
			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);
```

**File:** substrate/frame/treasury/src/lib.rs (L736-738)
```rust
		pub fn payout(origin: OriginFor<T>, index: SpendIndex) -> DispatchResult {
			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
```

**File:** substrate/frame/treasury/src/lib.rs (L742-745)
```rust
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);
```

**File:** substrate/frame/treasury/src/lib.rs (L747-752)
```rust
			let id = T::Paymaster::pay(&spend.beneficiary, spend.asset_kind.clone(), spend.amount)
				.map_err(|_| Error::<T, I>::PayoutError)?;

			spend.status = PaymentState::Attempted { id };
			spend.expire_at = now.saturating_add(T::PayoutPeriod::get());
			Spends::<T, I>::insert(index, spend);
```

**File:** substrate/frame/treasury/src/lib.rs (L778-805)
```rust
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::check_status())]
		pub fn check_status(origin: OriginFor<T>, index: SpendIndex) -> DispatchResultWithPostInfo {
			use PaymentState as State;
			use PaymentStatus as Status;

			ensure_signed(origin)?;
			let mut spend = Spends::<T, I>::get(index).ok_or(Error::<T, I>::InvalidIndex)?;
			let now = T::BlockNumberProvider::current_block_number();

			if now > spend.expire_at && !matches!(spend.status, State::Attempted { .. }) {
				// spend has expired and no further status update is expected.
				Spends::<T, I>::remove(index);
				Self::deposit_event(Event::<T, I>::SpendProcessed { index });
				return Ok(Pays::No.into());
			}

			let payment_id = match spend.status {
				State::Attempted { id } => id,
				_ => return Err(Error::<T, I>::NotAttempted.into()),
			};

			match T::Paymaster::check_payment(payment_id) {
				Status::Failure => {
					spend.status = PaymentState::Failed;
					Spends::<T, I>::insert(index, spend);
					Self::deposit_event(Event::<T, I>::PaymentFailed { index, payment_id });
				},
```

**File:** prdoc/stable2503/pr_7959.prdoc (L1-7)
```text
title: Update expire date on treasury payout
doc:
- audience: Runtime Dev
  description: |-
    Resets the `payout.expire_at` field with the `PayoutPeriod` every time that there is a valid Payout attempt.
    Prior to this change, when a spend is approved, it receives an expiry date so that if it’s never claimed, it automatically expires. This makes sense under normal circumstances. However, if someone attempts to claim a valid payout and there isn’t sufficient liquidity to fulfill it, the expiry date currently remains unchanged. This effectively penalizes the claimant in the same way as if they had never requested the payout in the first place.
    With this change users are not penalized for liquidity shortages and have a fair window to claim once the funds are available.
```

**File:** substrate/frame/treasury/src/tests.rs (L674-700)
```rust
#[test]
fn payout_extends_expiry() {
	ExtBuilder::default().build().execute_with(|| {
		assert_eq!(<Test as Config>::PayoutPeriod::get(), 5);

		System::set_block_number(1);
		assert_ok!(Treasury::spend(RuntimeOrigin::signed(10), Box::new(1), 2, Box::new(6), None));
		// Fail a payout at block 4
		System::set_block_number(4);
		assert_ok!(Treasury::payout(RuntimeOrigin::signed(1), 0));
		assert_eq!(paid(6, 1), 2);
		let payment_id = get_payment_id(0).expect("no payment attempt");
		// spend payment is failed
		set_status(payment_id, PaymentStatus::Failure);
		unpay(6, 1, 2);

		// check status to set the correct state
		assert_ok!(Treasury::check_status(RuntimeOrigin::signed(1), 0));
		System::assert_last_event(Event::<Test, _>::PaymentFailed { index: 0, payment_id }.into());

		// Retrying at after the initial expiry date but before the new one succeeds
		System::set_block_number(7);

		// the payout can be retried now
		assert_ok!(Treasury::payout(RuntimeOrigin::signed(1), 0));
		assert_eq!(paid(6, 1), 2);
	});
```
