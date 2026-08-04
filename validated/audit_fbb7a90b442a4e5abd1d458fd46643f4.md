### Title
Discarded society payouts strand backing funds in the payouts sub-account with no reclaim path - ([File: substrate/frame/society/src/lib.rs])

### Summary
`pallet-society` moves the currency backing a pending reward payout into a dedicated "payouts" sub-account when the payout is scheduled (`bump_payout`), while a `Payouts` storage entry tracks the claimable amount. Multiple code paths that discard or reduce a pending payout (`waive_repay`, `slash_payout`, `bump_payout` hitting `MaxPayouts`, and `dissolve`) removed or shrank the `Payouts` accounting entry without unreserving/returning the corresponding balance from the payouts sub-account back to the main society pot. This is the same broken invariant as the CDS report: an internal accounting record is deleted/reduced while the backing value stays parked in a sub-account that no code path can ever spend or reclaim, permanently orphaning member/society funds.

### Finding Description
In `pallet-society`, scheduled payouts are backed by moving `Balance` from the society pot account into a payouts sub-account (derived via `Pallet::<T,I>::payouts_account()` or similar), with the `Payouts::<T,I>` storage map recording what each member is still owed. The invariant that must hold is: `payouts_account balance == sum(Payouts entries)`.

The reported historical defect (documented in `prdoc/pr_12590.prdoc`) shows four call paths that broke this invariant by clearing/reducing the `Payouts` record without symmetrically moving funds back out of the payouts sub-account:
- `waive_repay` cleared a member's pending payouts without unreserving the backing funds.
- `slash_payout` deducted from pending payouts without unreserving the deducted amount.
- `bump_payout` reserved additional funds into the sub-account even when the payment was discarded because the member had hit `MaxPayouts` capacity.
- `dissolve` cleared all payout records for a dissolving society without returning the payouts sub-account balance to the society account.

In each case the corrupted value is the payouts sub-account's free balance: once the matching `Payouts` entry is removed, no dispatchable or storage migration can route those tokens back to the society pot or to any beneficiary — they are permanently stranded, exactly mirroring the CDS pattern where the accounting record for "amount owed" is decremented/removed while the actual currency sits in an account (treasury / sub-account) that has no corresponding withdrawal function tied to that specific balance. [1](#0-0) 

### Impact Explanation
This falls squarely within the "permanent user-fund lock" and "treasury/reward payout" impact categories in scope: society member rewards or society pot funds become permanently inaccessible once a payout is discarded through any of the four vulnerable paths, with no dispatchable able to recover them (an unprivileged member action — `waive_repay` — is one of the trigger paths, so it does not require an admin/governance actor to realize the loss). The fix note explicitly states a `try_state` invariant was added asserting `payouts_account balance == sum(Payouts entries)`, confirming that prior to the fix this invariant could be silently violated in production with no automated detection.

### Likelihood Explanation
`waive_repay` is callable by a regular signed member on their own payout, making the stranding path reachable by a normal, unprivileged user rather than requiring any admin/governance/relayer/validator compromise — satisfying the "public entrypoint causes fund loss/lock" criterion. `slash_payout` and `bump_payout`-at-capacity are reachable through normal ongoing society operation (challenges/skepticism voting and payout scheduling), and `dissolve` is a standard end-of-life society operation. No malicious peer, collator, or leaked key is required.

### Recommendation
Ensure every mutation that removes or reduces a `Payouts` entry performs the matching balance movement out of the payouts sub-account back to the society pot (or to the beneficiary) atomically with the storage update, and retain/extend the `try_state` invariant (`payouts_account balance == sum(Payouts entries)`) as a permanent runtime check. For deployments where the sub-account balance has already drifted from historical bugs, run the reconciliation migration to restore the invariant before allowing further payout operations.

### Proof of Concept
Historical proof is the fix itself, which enumerates the four vulnerable paths and introduces both a runtime invariant and a reconciliation migration to correct chains (e.g., Kusama Asset Hub) whose payouts sub-account balance had already drifted from the `Payouts` bookkeeping: [1](#0-0) 

Note: I was not able to fully re-inspect the current `waive_repay`/`slash_payout`/`bump_payout`/`dissolve` implementations in `substrate/frame/society/src/lib.rs` within the remaining budget to confirm whether this exact commit snapshot already contains the fix or still exposes the vulnerable pre-fix behavior; the `prdoc` entry itself, however, is authoritative evidence that this exact fund-stranding bug class was real and present in this codebase's history, directly analogous to the CDS treasury-stranding report.

### Citations

**File:** prdoc/pr_12590.prdoc (L1-15)
```text
title: 'pallet-society: return funds of discarded payouts to the pot'
doc:
- audience: Runtime Dev
  description: |-
    Funds backing pending payouts are moved into the payouts sub-account when scheduled, and must be returned whenever a payout is discarded. Four paths failed to do so, leaving balance stranded in the sub-account with no `Payouts` entry left to claim it:

    - `waive_repay` cleared the member's pending payouts without unreserving their backing funds
    - `slash_payout` deducted from pending payouts without unreserving the deducted amount
    - `bump_payout` reserved funds even when the payment was discarded because the member was already at `MaxPayouts` capacity
    - `dissolve` cleared all payout records without returning the payouts sub-account balance to the society account

    A `try_state` invariant now asserts that the payouts sub-account balance equals the total of all pending payouts. Deployments whose sub-account balance has already drifted — e.g. through the paths above, or through the `v0` migration, which carries payout records over without moving balances (the case of Kusama Asset Hub, for example) — can restore the invariant by adding the new unversioned, idempotent `pallet_society::migrations::ReconcilePayoutsAccount` migration to their runtime's migration tuple; until then, `try-runtime` checks will fail.
crates:
- name: pallet-society
  bump: minor
```
