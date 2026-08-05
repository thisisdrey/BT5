### Title
Sync-committee liveness lock in Snowbridge Ethereum light client: `submit` cannot recover once a period's rotation update is missed, requiring root `force_checkpoint` re-bootstrap - (File: `bridges/snowbridge/pallets/ethereum-client/src/lib.rs`)

### Summary
The Compound Open Oracle bug boils down to: a single, permissionless-progress mechanism (`postPrices`) is hard-wired to accept only messages tied to one specific signing identity (the reporter). Once that identity is invalidated/unusable, no unprivileged party can move state forward — the view is stuck unless a brand-new signed message from that exact identity appears, and if none ever comes, the contract must be re-deployed. The exact analog in this repo is `snowbridge-pallet-ethereum-client`'s beacon light client: forward progress via the public `submit` extrinsic is hard-wired to accept updates only for the `store_period` or `store_period + 1` (`bridges/snowbridge/pallets/ethereum-client/src/lib.rs:311-461`), gated on possessing a `NextSyncCommittee` for the *specific* rotation window. If no relayer submits the "next sync committee" update inside that single-period window, the store falls permanently behind (`Error::SkippedSyncCommitteePeriod`), and no signed update from any future, valid sync committee can ever be accepted again through the public path — only a privileged `force_checkpoint` (root origin) can unstick it, exactly mirroring the oracle's "re-deploy" fallback.

### Finding Description
`Pallet::submit` (`bridges/snowbridge/pallets/ethereum-client/src/lib.rs:220-224`) is the only permissionless (any signed account) path to advance the light client's finalized head. It calls `process_update` → `verify_update` (lines 302-461).

Key invariant enforced in `verify_update`:
```
let store_period = compute_period(latest_finalized_state.slot);
let signature_period = compute_period(update.signature_slot);
if <NextSyncCommittee<T>>::exists() {
    ensure!((store_period..=store_period + 1).contains(&signature_period), Error::<T>::SkippedSyncCommitteePeriod)
} else {
    ensure!(signature_period == store_period, Error::<T>::SkippedSyncCommitteePeriod)
}
``` [1](#0-0) 

Progressing the store to a new period is only possible if a valid `next_sync_committee_update` was accepted for the *current* store period while it was still current:
```
if !<NextSyncCommittee<T>>::exists() {
    ensure!(update_finalized_period == store_period, <Error<T>>::InvalidSyncCommitteeUpdate);
    <NextSyncCommittee<T>>::set(sync_committee_prepared);
} else if update_finalized_period == store_period + 1 {
    <CurrentSyncCommittee<T>>::set(<NextSyncCommittee<T>>::get());
    <NextSyncCommittee<T>>::set(sync_committee_prepared);
}
``` [2](#0-1) 

If the real Ethereum beacon chain's period advances by more than one period without any relayer having submitted a `next_sync_committee_update` for the exact period the store was in at the time (i.e. `<NextSyncCommittee<T>>` was never set for that window, and by the time anyone tries again `store_period` is already stale relative to `signature_period`), the `SkippedSyncCommitteePeriod` check permanently rejects all future submissions: `signature_period` will never again satisfy `signature_period == store_period` (no committee update case) nor `(store_period..=store_period+1)` (existing-next-committee case), because `store_period` is frozen in storage and only advances via the very update path that is now blocked. This is confirmed as a real, previously-fixed class of bug: `prdoc/1.13.0/pr_4478.prdoc` documents "Reject finalized updates without a sync committee in next store period... stalls the light client when an update in the next sync committee period is received without receiving the next sync committee update in the next period" [3](#0-2) . The recovery path in the module doc explicitly requires a privileged, root-only call:
```
//! ## Governance
//! * [`Call::force_checkpoint`]: Set the initial trusted consensus checkpoint.
``` [4](#0-3)  and `force_checkpoint` is `ensure_root`-gated [5](#0-4) .

This is the direct structural analog of the oracle bug: a single monotonic piece of authorization state (`store_period`/`NextSyncCommittee`, vs. the oracle's "reporter" identity) gates the only permissionless update path, and once the narrow window to refresh that state is missed, no valid signed data from the legitimate (rotated) signer set can restore liveness — only a privileged intervention (root `force_checkpoint`, vs. the oracle's "re-deploy") can recover it.

### Impact Explanation
This falls under "public underpriced work that degrades block production or stalls bridge processing" and "permanent... bridge-state lock" per the impact gate. Once the store is permanently stuck at a stale period, all subsequent Ethereum→Substrate message verification relying on the beacon client's finalized header state (execution header proofs, ancestry proofs used by Snowbridge's inbound message queue) can no longer advance, effectively halting bridge inbound processing until governance manually re-bootstraps via `force_checkpoint`, which requires re-establishing full trust in a new checkpoint (a heavier, privileged action, analogous to redeploying the oracle view).

### Likelihood Explanation
The trigger does not require a malicious relayer, attacker, or governance actor — it only requires that no relayer (of potentially many, permissionless, unprivileged callers of `submit`) happens to submit a valid `next_sync_committee_update` for the specific period boundary before the real Ethereum chain's period advances further. Relayer downtime, cost-optimization skipping of "free interval" updates, or a race between when a period starts and slow submission are enough. The precise off-by-one/period-skip failure mode was serious enough to already be identified and partially patched (`pr_4478`), indicating the state machine's assumptions about update cadence are fragile and this exact strict-window liveness assumption is realistic to hit again given only the code shown checks `store_period` vs `store_period+1` without any correction path once desynchronized beyond one period, or once the free/`NextSyncCommittee` window is missed under the current-file logic.

### Recommendation
Add a permissionless recovery path that does not require re-establishing full root trust: e.g., allow submission of a "long-range" sync-committee update proof chain (successive period committee handoffs) so any signed, valid update chain from the last known committee can walk the store forward past skipped periods, rather than hard rejecting any `signature_period` beyond `store_period + 1`. Alternatively, make period-skip recovery available through a permissionless, proof-verified "catch-up" call rather than solely through `ensure_root`-gated `force_checkpoint`.

### Proof of Concept
1. Deploy/bootstrap the pallet via `force_checkpoint` at Ethereum sync-committee period `P`.
2. Ensure `NextSyncCommittee` is never set for period `P` (no relayer submits a valid `next_sync_committee_update` while `store_period == P`).
3. Let the real Ethereum beacon chain advance to period `P+2` (two full sync-committee rotations pass).
4. Any relayer calls `submit` with a fully valid, correctly BLS-signed update for period `P+1` or `P+2`: `verify_update` computes `signature_period` as `P+1`/`P+2`; since `<NextSyncCommittee<T>>::exists()` is false, the check `ensure!(signature_period == store_period, Error::<T>::SkippedSyncCommitteePeriod)` fails because `store_period` is still `P`.
5. No further valid, honestly-signed update can ever satisfy the period-continuity check because `store_period` can only be advanced by the same gated `submit` path — the store is permanently frozen at period `P`, matching the oracle bug's "must re-deploy" end state; only root-only `force_checkpoint` can recover it.

### Citations

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L7-11)
```rust
//! # Extrinsics
//!
//! ## Governance
//!
//! * [`Call::force_checkpoint`]: Set the initial trusted consensus checkpoint.
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L196-208)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::force_checkpoint())]
		#[transactional]
		/// Used for pallet initialization and light client resetting. Needs to be called by
		/// the root origin.
		pub fn force_checkpoint(
			origin: OriginFor<T>,
			update: Box<CheckpointUpdate>,
		) -> DispatchResult {
			ensure_root(origin)?;
			Self::process_checkpoint_update(&update)?;
			Ok(())
		}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L327-336)
```rust
			let store_period = compute_period(latest_finalized_state.slot);
			let signature_period = compute_period(update.signature_slot);
			if <NextSyncCommittee<T>>::exists() {
				ensure!(
					(store_period..=store_period + 1).contains(&signature_period),
					Error::<T>::SkippedSyncCommitteePeriod
				)
			} else {
				ensure!(signature_period == store_period, Error::<T>::SkippedSyncCommitteePeriod)
			}
```

**File:** bridges/snowbridge/pallets/ethereum-client/src/lib.rs (L487-496)
```rust
				if !<NextSyncCommittee<T>>::exists() {
					ensure!(
						update_finalized_period == store_period,
						<Error<T>>::InvalidSyncCommitteeUpdate
					);
					<NextSyncCommittee<T>>::set(sync_committee_prepared);
				} else if update_finalized_period == store_period + 1 {
					<CurrentSyncCommittee<T>>::set(<NextSyncCommittee<T>>::get());
					<NextSyncCommittee<T>>::set(sync_committee_prepared);
				}
```

**File:** prdoc/1.13.0/pr_4478.prdoc (L1-13)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: Snowbridge - Ethereum Client - Reject finalized updates without a sync committee in next store period

doc:
  - audience: Runtime Dev
    description: |
      Bug fix in the Ethereum light client that stalls the light client when an update in the next sync committee period is received without receiving the next sync committee update in the next period.

crates:
  - name: snowbridge-pallet-ethereum-client
    bump: patch
```
