Audit Report

## Title
Silent payout-transfer failure in `claim_bounty`/`claim_child_bounty` permanently strands treasury funds - (File: `substrate/frame/bounties/src/lib.rs`)

## Summary
`claim_bounty` computes `payout = balance.saturating_sub(fee)` and calls `T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath)`, checking the result only via `debug_assert!(res.is_ok())`, then unconditionally clears the bounty via `*maybe_bounty = None;` regardless of whether the transfer succeeded. [1](#0-0)  The identical pattern exists in `claim_child_bounty`, which uses `debug_assert!` on both the fee transfer and the beneficiary transfer before the child-bounty record is finalized. [2](#0-1) 

## Finding Description
`debug_assert!` compiles to a no-op in release builds, so any `Err` returned by `T::Currency::transfer` is discarded without aborting the extrinsic, matching the reported pattern of a value-moving call failing while surrounding logic proceeds as if it succeeded. [3](#0-2)  The balances pallet enforces that a new account cannot receive a deposit below the `ExistentialDeposit`, returning `Error::<T,I>::ExistentialDeposit` when `value < ed && is_new`. [4](#0-3)  Consequently `transfer(..., payout, AllowDeath)` returns `Err` whenever the beneficiary account does not yet exist and `payout` (which can be driven to a very low or zero value via fee sizing or slashing of the bounty sub-account) falls below `ExistentialDeposit`. Since `claim_bounty` is callable by any signed account (`ensure_signed(origin)?; // anyone can trigger claim`), [5](#0-4)  the extrinsic returns `Ok(())`, the bounty storage record is deleted, and the leftover balance remains trapped in the bounty sub-account with no on-chain index left to reclaim it.

## Impact Explanation
This causes permanent, unrecoverable loss of treasury-sourced funds through ordinary protocol usage (curator fee sizing or slashing edge cases), without requiring privileged access, matching the "permanent user-fund lock" category of accepted impacts.

## Likelihood Explanation
Triggering this requires the beneficiary account to be new/non-existent and the computed `payout` to fall below `ExistentialDeposit` — achievable via fee sizing or bounty-account slashing, as demonstrated by the repository's own `claim_handles_high_fee` test which drives `payout` to `0`. [6](#0-5)  No malicious actor or privileged role is needed; any signed account can trigger the claim once the bounty is `PendingPayout`.

## Recommendation
Replace the `debug_assert!(res.is_ok())` checks in `claim_bounty` and `claim_child_bounty` with proper error propagation (`?`/`ensure!`) so the extrinsic reverts and preserves the bounty record on transfer failure, or explicitly handle the below-ED remainder (e.g., top up to ED, or route the shortfall to the treasury account) before clearing bounty/child-bounty storage.

## Proof of Concept
1. Propose and approve a bounty of value `V`; assign a curator with `fee` close to `V` such that `payout = V - fee < ExistentialDeposit`.
2. Set the beneficiary to a fresh `AccountId` that has never held a balance.
3. Wait past `unlock_at` and call `claim_bounty` from any signed account.
4. In a release build, `T::Currency::transfer(...)` returns `Err(Error::ExistentialDeposit)`, the `debug_assert!` is compiled out, `*maybe_bounty = None` still executes, and `BountyClaimed` fires — but `payout` remains permanently locked in `bounty_account_id(bounty_id)`, mirroring the existing `claim_handles_high_fee` test setup [7](#0-6)  but with a non-existent beneficiary account instead of a pre-funded one.

### Citations

**File:** substrate/frame/bounties/src/lib.rs (L799-800)
```rust
		) -> DispatchResult {
			ensure_signed(origin)?; // anyone can trigger claim
```

**File:** substrate/frame/bounties/src/lib.rs (L820-828)
```rust
					let final_fee = fee.saturating_sub(children_fee);
					let res =
						T::Currency::transfer(&bounty_account, &curator, final_fee, AllowDeath); // should not fail
					debug_assert!(res.is_ok());
					let res =
						T::Currency::transfer(&bounty_account, &beneficiary, payout, AllowDeath); // should not fail
					debug_assert!(res.is_ok());

					*maybe_bounty = None;
```

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());
```

**File:** substrate/frame/balances/src/impl_currency.rs (L494-496)
```rust
			|account, is_new| -> Result<Self::PositiveImbalance, DispatchError> {
				let ed = T::ExistentialDeposit::get();
				ensure!(value >= ed || !is_new, Error::<T, I>::ExistentialDeposit);
```

**File:** substrate/frame/bounties/src/tests.rs (L889-920)
```rust
#[test]
fn claim_handles_high_fee() {
	ExtBuilder::default().build_and_execute(|| {
		Balances::make_free_balance_be(&Treasury::account_id(), 101);
		Balances::make_free_balance_be(&4, 30);
		assert_ok!(Bounties::propose_bounty(RuntimeOrigin::signed(0), 50, b"12345".to_vec()));

		assert_ok!(Bounties::approve_bounty(RuntimeOrigin::root(), 0));

		go_to_block(2);

		assert_ok!(Bounties::propose_curator(RuntimeOrigin::root(), 0, 4, 49));
		assert_ok!(Bounties::accept_curator(RuntimeOrigin::signed(4), 0));

		assert_ok!(Bounties::award_bounty(RuntimeOrigin::signed(4), 0, 3));

		go_to_block(5);

		// make fee > balance
		let res = Balances::slash(&Bounties::bounty_account_id(0), 10);
		assert_eq!(res.0.peek(), 10);

		assert_ok!(Bounties::claim_bounty(RuntimeOrigin::signed(1), 0));

		assert_eq!(
			last_event(),
			BountiesEvent::BountyClaimed { index: 0, payout: 0, beneficiary: 3 }
		);

		assert_eq!(Balances::free_balance(4), 70); // 30 + 50 - 10
		assert_eq!(Balances::free_balance(3), 0);
		assert_eq!(Balances::free_balance(Bounties::bounty_account_id(0)), 0);
```
