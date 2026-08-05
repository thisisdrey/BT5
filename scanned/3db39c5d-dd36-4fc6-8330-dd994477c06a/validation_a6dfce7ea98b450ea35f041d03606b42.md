## Analysis

The external report's core broken invariant: **a strict timing-window boundary check (`X < now` / `X > now`) with no buffer causes users to permanently forfeit funds they are otherwise entitled to, if they do not submit a claim/withdrawal transaction inside an exact, easy-to-miss window.**

The closest local analog is `pallet-treasury`'s `spend` / `payout` / `check_status` flow, which implements exactly this pattern: an approved payment is only claimable inside `[valid_from, expire_at)`, and once that window is missed, the entitlement is permanently pruned with no recovery path other than a fresh governance approval.

### Title
Treasury approved spends are irrecoverably lost if `payout` is not submitted inside the strict `expire_at` window - (File: `substrate/frame/treasury/src/lib.rs`)

### Summary
`pallet-treasury::spend` creates a `SpendStatus` with a `valid_from`/`expire_at` window sized by `Config::PayoutPeriod`. `payout` enforces `now >= spend.valid_from` and `spend.expire_at > now` with no grace buffer. If the beneficiary (or anyone signed, since `payout` has no check that the caller is the beneficiary) does not submit `payout` before `expire_at`, `check_status` permanently removes the `Spends` entry and the approved amount is never paid — mirroring the ERC20Airdrop2 bug class where a hard cutoff with no buffer causes forfeiture of an amount the user was otherwise entitled to receive.

### Finding Description
`spend` stores the claim window: [1](#0-0) 

`payout` strictly rejects any claim attempt once `now >= expire_at`, with no buffer: [2](#0-1) 

`check_status` is the enforcement mechanism that prunes the spend once expired, permanently discarding the entitlement (funds are simply never paid; the approved allocation is dropped from state): [3](#0-2) 

This is structurally identical to `ERC20Airdrop2`'s `ongoingWithdrawals` modifier: a strict inequality (`claimEnd + withdrawalWindow < block.timestamp`) that provides zero buffer for a legitimate, non-malicious actor to time their claim, resulting in forfeiture of funds they were otherwise fully entitled to. In both cases the fix acknowledged by the maintainers was "add a buffer window" — Taiko's PR 16596, and Polkadot's own partial acknowledgment of the same defect class in `pr_7959.prdoc`, which only extends `expire_at` on a *failed payout attempt*, not on the more common case where the beneficiary simply never submits a `payout` extrinsic before `expire_at` (e.g., due to chain congestion, wallet downtime, or the beneficiary being an unattended multisig/pallet account): [4](#0-3) 

Existing guards do not stop this path because:
- There is no re-approval or automatic retry once `expire_at` passes; the only remedy is a brand-new `SpendOrigin` approval, which is a distinct governance action, not a recovery of the original entitlement.
- `payout` has no origin-restriction tying it to the beneficiary (`ensure_signed(origin)?` only), so this is not a matter of the beneficiary being unable to act — any signed account could submit it, yet the fixed, buffer-less window still causes permanent loss if nobody does so in time.
- `check_status` unconditionally deletes the storage entry once `now > expire_at` (outside of an in-flight `Attempted` payment), which is the "forfeiture" event — analogous to the airdrop's post-window revert.

### Impact Explanation
A treasury-approved spend (real DOT/asset value, since `spend` requires `SpendOrigin` sign-off representing a genuine budget allocation) is permanently and unrecoverably lost from the beneficiary's perspective the moment `expire_at` is crossed without a submitted `payout`. This matches "permanent user-fund … lock" impact class: value that should have settled to the rightful beneficiary is dropped from state with no exact-timing safety margin, purely due to a hard boundary check with zero buffer.

### Likelihood Explanation
Moderate-to-high. `PayoutPeriod` is a fixed runtime constant (not adjustable per spend beyond the initial `valid_from` offset), and there is no protection against transient conditions (congestion, downtime, delayed notification of approval) preventing submission in time. Any legitimate, non-malicious beneficiary who is simply late — even by one block relative to `expire_at` — loses the entire spend, with no partial-forgiveness or buffer, exactly the scenario the original report demonstrates for airdrop claims.

### Recommendation
Introduce a buffer/grace window analogous to the airdrop fix: e.g., allow `payout` to succeed for some additional grace period past `expire_at` before `check_status` is permitted to prune the entry, or auto-extend `expire_at` when the spend has never been attempted at all (not just on failed attempts, as `pr_7959` currently does). Alternatively, make `check_status`'s pruning path emit a recoverable state (e.g., re-queue for re-approval automatically) rather than unconditionally destroying the claim record.

### Proof of Concept
1. `SpendOrigin` approves `Treasury::spend(asset_kind, amount, beneficiary, None)` at block `N`. This sets `valid_from = N`, `expire_at = N + PayoutPeriod`.
2. No one calls `payout(index)` before block `N + PayoutPeriod` (e.g., beneficiary's infrastructure is down, or governance takes time to notify the beneficiary).
3. At block `> N + PayoutPeriod`, anyone calls `Treasury::payout(index)` → reverts with `Error::SpendExpired` (per `substrate/frame/treasury/src/lib.rs:741`, confirmed by the existing test `spend_expires` at lines 632-648 of `substrate/frame/treasury/src/tests.rs`).
4. Anyone calls `check_status(index)` → the `Spends` entry is deleted (`substrate/frame/treasury/src/lib.rs:786-793`), permanently discarding the beneficiary's entitlement with no buffer or recovery mechanism, exactly mirroring Alice's forfeited airdrop tokens in the original C4 finding. [5](#0-4)

### Citations

**File:** substrate/frame/treasury/src/lib.rs (L661-664)
```rust
			let now = T::BlockNumberProvider::current_block_number();
			let valid_from = valid_from.unwrap_or(now);
			let expire_at = valid_from.saturating_add(T::PayoutPeriod::get());
			ensure!(expire_at > now, Error::<T, I>::SpendExpired);
```

**File:** substrate/frame/treasury/src/lib.rs (L739-745)
```rust
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now >= spend.valid_from, Error::<T, I>::EarlyPayout);
			ensure!(spend.expire_at > now, Error::<T, I>::SpendExpired);
			ensure!(
				matches!(spend.status, PaymentState::Pending | PaymentState::Failed),
				Error::<T, I>::AlreadyAttempted
			);
```

**File:** substrate/frame/treasury/src/lib.rs (L786-793)
```rust
			let now = T::BlockNumberProvider::current_block_number();

			if now > spend.expire_at && !matches!(spend.status, State::Attempted { .. }) {
				// spend has expired and no further status update is expected.
				Spends::<T, I>::remove(index);
				Self::deposit_event(Event::<T, I>::SpendProcessed { index });
				return Ok(Pays::No.into());
			}
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

**File:** substrate/frame/treasury/src/tests.rs (L632-648)
```rust
#[test]
fn spend_expires() {
	ExtBuilder::default().build().execute_with(|| {
		assert_eq!(<Test as Config>::PayoutPeriod::get(), 5);

		// spend `0` expires in 5 blocks after the creating.
		System::set_block_number(1);
		assert_ok!(Treasury::spend(RuntimeOrigin::signed(10), Box::new(1), 2, Box::new(6), None));
		System::set_block_number(6);
		assert_noop!(Treasury::payout(RuntimeOrigin::signed(1), 0), Error::<Test, _>::SpendExpired);

		// spend cannot be approved since its already expired.
		assert_noop!(
			Treasury::spend(RuntimeOrigin::signed(10), Box::new(1), 2, Box::new(6), Some(0)),
			Error::<Test, _>::SpendExpired
		);
	});
```
