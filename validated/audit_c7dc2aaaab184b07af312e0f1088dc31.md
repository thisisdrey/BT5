### Title
Unchecked balance transfer results in `claim_child_bounty` cause silent-failure payouts and permanent fund lock - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`pallet-child-bounties::claim_child_bounty` transfers the curator fee and the beneficiary payout from the child-bounty sovereign account and only guards the results with `debug_assert!`, exactly the same failure-to-verify-transfer pattern flagged in the external report for `RelayPolymarketSDK::transferUsdc()`. Because `debug_assert!` compiles to a no-op in release builds (the build profile used for production runtimes), a failed transfer is silently ignored, the bounty record is still deleted from storage, and a `Claimed` event is still emitted as if the payout succeeded.

### Finding Description
In `claim_child_bounty`, both payments are performed and checked like this: [1](#0-0) 

```
// Make payout to child-bounty curator.
// Should not fail because curator fee is always less than bounty value.
let fee_transfer_result = T::Currency::transfer(&child_bounty_account, curator, curator_fee, AllowDeath);
debug_assert!(fee_transfer_result.is_ok());

// Make payout to beneficiary.
// Should not fail.
let payout_transfer_result = T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath);
debug_assert!(payout_transfer_result.is_ok());

Self::deposit_event(Event::<T>::Claimed { index: parent_bounty_id, child_index: child_bounty_id, payout, beneficiary: beneficiary.clone() });

*maybe_child_bounty = None; // storage entry is unconditionally removed
Ok(())
```

`debug_assert!` is stripped out unless `debug_assertions` is enabled, which is not the case for standard `--release` runtime builds used in production chains. This means the `Result` returned by `T::Currency::transfer` is effectively discarded on any real network: if either transfer fails for any reason (e.g., the destination account is subject to `TooManyConsumers`/provider-ref limits on creation, a hold/freeze interaction, or any other `DispatchError` from the currency implementation), execution proceeds exactly as if the transfer had succeeded.

The same anti-pattern of using `debug_assert!`/`is_ok()` best-effort checks after transferring value exists in `pallet-tips::payout_tip` as well: [2](#0-1) , reinforcing that this is a recurring, intentional-but-unsafe pattern in this codebase rather than an isolated typo.

The consequence in `claim_child_bounty` is worse than in `payout_tip` because the child-bounty's tracking state (`ChildBounties`, `ChildBountyDescriptionsV1`, `ParentChildBounties` counters) is deleted unconditionally right after the (possibly failed) transfers, and a `Claimed` event with the intended `payout` amount is emitted regardless of whether funds actually moved.

### Impact Explanation
If the payout transfer silently fails:
- The `Claimed` event falsely reports that `beneficiary` received `payout`, corrupting on-chain/off-chain accounting (indexers, dashboards, downstream automation) with an unbacked settlement record.
- The child-bounty's dedicated sovereign account (`child_bounty_account_id(parent_bounty_id, child_bounty_id)`) retains the funds, but the storage record proving those funds are earmarked for `beneficiary`/`curator` is deleted (`*maybe_child_bounty = None;` and description removal), and `ParentChildBounties` count is decremented as if the bounty is fully settled.
- Because there is no remaining on-chain link from the now-orphaned sovereign account back to the intended recipients, the funds become effectively stranded/unrecoverable through the normal bounty-claim path — a permanent user-fund lock, matching the "permanent user-fund or bridge-state lock" and "settle exactly once to the rightful beneficiary and amount" impact classes in the gate.

### Likelihood Explanation
`claim_child_bounty` is a fully public, unprivileged extrinsic (only `ensure_signed` is required, no origin restriction to curator/beneficiary), so any account can trigger it once the child bounty is in `PendingPayout` state. Triggering an actual transfer failure requires a specific, non-adversary-controlled precondition on the currency implementation (e.g., an account hitting provider/consumer limits, or a hold/lock edge case introduced by other pallets in a given runtime), which is why likelihood is assessed as low-to-moderate rather than trivially reproducible on every runtime — but the guard (`debug_assert!`) provides zero protection in production regardless of how the failure occurs.

### Recommendation
Replace the `debug_assert!` checks with proper error propagation (e.g., `?` or `map_err` returning a dedicated `Error::<T>::PayoutFailed`), and only delete/mutate the child-bounty storage entries and emit the `Claimed` event after both transfers have been confirmed to succeed, mirroring the pattern already used correctly elsewhere in the codebase (e.g. `T::Currency::transfer(...).defensive().map_err(...)?` in `cumulus/pallets/ah-ops/src/lib.rs`, lines 537-539) [3](#0-2) . Apply the same fix to `pallet-tips::payout_tip`.

### Proof of Concept
1. Set up a runtime where `T::Currency` (e.g., `pallet-balances` combined with a companion pallet enforcing provider/consumer-ref limits, or a currency wrapper that can reject a transfer to a specific account under some condition — e.g., the destination already at `MaxConsumers`).
2. Create a bounty, add and approve a child bounty, assign a curator, and advance it to `PendingPayout` with a beneficiary account engineered to reject the incoming transfer (e.g., an account already at the consumer-ref ceiling so the implicit `frame_system::inc_consumers` performed by `transfer` fails).
3. Call `claim_child_bounty(origin, parent_bounty_id, child_bounty_id)` from any signed account.
4. Observe (in a release-profile build, i.e. `debug_assertions` disabled): `T::Currency::transfer` for the beneficiary returns `Err(_)`, but the call still returns `Ok(())`, emits `Event::Claimed { payout, beneficiary, .. }`, and deletes the child-bounty storage — while the funds remain stuck in `child_bounty_account_id(parent_bounty_id, child_bounty_id)` with no more on-chain record connecting them to `beneficiary`.

### Citations

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

**File:** substrate/frame/tips/src/lib.rs (L587-600)
```rust
		if tip.finders_fee && tip.finder != tip.who {
			// pay out the finder's fee.
			let finders_fee = T::TipFindersFee::get() * payout;
			payout -= finders_fee;
			// this should go through given we checked it's at most the free balance, but still
			// we only make a best-effort.
			let res = T::Currency::transfer(&treasury, &tip.finder, finders_fee, KeepAlive);
			debug_assert!(res.is_ok());
		}

		// same as above: best-effort only.
		let res = T::Currency::transfer(&treasury, &tip.who, payout, KeepAlive);
		debug_assert!(res.is_ok());
		Self::deposit_event(Event::TipClosed { tip_hash: hash, who: tip.who, payout });
```

**File:** cumulus/pallets/ah-ops/src/lib.rs (L536-539)
```rust
			// Now the actual balance transfer to the new account
			<T as Config>::Currency::transfer(from, to, total, Preservation::Expendable)
				.defensive()
				.map_err(|_| Error::<T>::FailedToTransfer)?;
```
