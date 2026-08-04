### Title
Root-origin governance dispatch cannot execute native-value contract calls, permanently blocking proposals that require sending native coins - (File: `substrate/frame/contracts/src/exec.rs`, `substrate/frame/revive/src/exec.rs`)

### Summary
The external report describes an `InterchainProposalExecutor` that cannot forward native coin value because it is invoked from a non-payable wrapper, so any governance proposal that requires sending native funds can never be executed. The Substrate equivalent of this broken invariant exists in `pallet-contracts` and `pallet-revive`: when a call is dispatched with `RawOrigin::Root` (the origin used by OpenGov "Root track" referenda, `pallet-scheduler`, and sudo-style enactments) and that call carries a non-zero native `value`, the pallet unconditionally rejects the transfer with `Error::RootNotAllowed`, because `Origin::Root` has no backing account to debit funds from.

### Finding Description
`Contracts::call` (`substrate/frame/contracts/src/lib.rs:938`) and `Pallet::<T>::call` in `pallet-revive` (`substrate/frame/revive/src/lib.rs:1171`) both accept an arbitrary `value: BalanceOf<T>` parameter and route execution through the exec stack. Inside `substrate/frame/contracts/src/exec.rs:1190-1211`, `initial_transfer` performs the actual balance movement: [1](#0-0) 

```rust
let value = frame.value_transferred;
let caller = match self.caller() {
    Origin::Signed(caller) => caller,
    Origin::Root if value.is_zero() => return Ok(()),
    Origin::Root => return DispatchError::RootNotAllowed.into(),
};
```

If the call is dispatched from `Origin::Root` and `value` is non-zero, the call deterministically fails with `RootNotAllowed`. The same guard pattern exists in `pallet-revive`'s `exec.rs` (`RootNotAllowed` is referenced there as well). This is by design for direct extrinsic submission (Root cannot sign a debit), but it becomes a functional blocker when the call is the *target* of a governance-enacted `Root`-origin dispatch: any `pallet-referenda` proposal on the Root track (or any `Scheduler`/`sudo`-style enactment using `RawOrigin::Root`) that is meant to instruct a contract to receive native funds — e.g. funding a multisig/vesting contract, seeding a pool, or paying out a bounty through a contract call — cannot be executed at all. There is no code path that lets Root supply value; the only options are (a) never use value in Root-dispatched contract calls, or (b) accept that the proposal always fails post-approval.

This mirrors exactly the Axelar bug class: the "wrapper" (`Root`-origin dispatch layer used by governance) cannot carry native value into the downstream call (`pallet_contracts::call` / `pallet_revive::call`), so any proposal requiring a native-value contract call is permanently non-executable once approved by governance — the transaction can be resubmitted indefinitely and will always revert with the same error, since the fault is structural, not transient.

### Impact Explanation
This falls under "runtime bugs that compromise intended behavior": a legitimately-approved, non-malicious governance proposal (Root track referendum, or any Root-dispatch scheduling flow) that legitimately requires transferring native currency into a contract as part of its call cannot be enacted. The proposal is queued, approved, scheduled, and dispatched, then fails deterministically at execution time with no recovery path other than re-authoring the proposal to route funds through a separate mechanism first — which reintroduces the same "pre-fund then race" problem called out in the original report (funds sitting in an intermediate account can be consumed/reordered independently of the intended atomic proposal). This is a chain-level guarantee failure (approved governance intent cannot be executed atomically), not a mere inconvenience.

### Likelihood Explanation
No privileged or malicious actor is required beyond the normal governance flow (which is explicitly excluded from being counted as "root cause" here — the root cause is the missing value path in the exec stack, not governance abuse). Any Root-track referendum author who writes a proposal containing `Contracts::call(..., value: N, ...)` or `Revive::call(..., value: N, ...)` with `N > 0` will hit this deterministically the moment the proposal is enacted, on any relay/parachain that has `pallet-contracts` or `pallet-revive` and dispatches proposals via `RawOrigin::Root` (standard in current polkadot-sdk governance configurations).

### Recommendation
Provide an explicit, funded-and-metered path for Root-origin value transfers into contracts, analogous to the Axelar team's accepted mitigation (fund an intermediary/agent account and settle atomically within the same transaction) rather than relying on a bare `Origin::Root` debit. Concretely: either (a) introduce a Root-callable extrinsic variant that resolves the transfer via an on-chain treasury/vault account rather than attempting to debit a nonexistent Root account, ensuring atomic funding+execution in a single dispatch, or (b) surface a clear governance-time check (e.g., a `Call` filter or dry-run simulation in `pallet-referenda`) that rejects proposals containing non-zero-value Root-origin contract calls before they are allowed onto a track, so proposal authors are not able to submit an intent that can never execute.

### Proof of Concept
1. Configure a runtime with `pallet-referenda` (or `pallet-scheduler`) able to dispatch calls with `RawOrigin::Root`, and with `pallet-contracts` (or `pallet-revive`) enabled.
2. Submit and pass a Root-track referendum whose enacted call is `Contracts::call { dest: <contract>, value: 1_000_000_000, gas_limit: ..., data: ... }`.
3. Observe that at enactment time `initial_transfer` in `substrate/frame/contracts/src/exec.rs` matches `Origin::Root` with non-zero `value` and returns `DispatchError::RootNotAllowed`, causing the scheduled/enacted call to fail every time it is (re)tried, permanently blocking the proposal's intended native-fund transfer.

### Citations

**File:** substrate/frame/contracts/src/exec.rs (L1200-1211)
```rust
		let value = frame.value_transferred;

		// Get the account id from the caller.
		// If the caller is root there is no account to transfer from, and therefore we can't take
		// any `value` other than 0.
		let caller = match self.caller() {
			Origin::Signed(caller) => caller,
			Origin::Root if value.is_zero() => return Ok(()),
			Origin::Root => return DispatchError::RootNotAllowed.into(),
		};
		Self::transfer(Preservation::Preserve, &caller, &frame.account_id, value)
	}
```
