### Title
GRANDPA bridge light client (`pallet-bridge-grandpa`) becomes permanently stuck and irrecoverable by unprivileged users if the bridged chain issues a forced authority-set change or a delayed scheduled change - ([File: bridges/modules/grandpa/src/lib.rs])

### Summary
`pallet-bridge-grandpa` is the on-chain GRANDPA light client that other bridge pallets (parachains pallet, messages pallet) depend on to verify storage proofs of the bridged chain. Progress of the light client absolutely requires importing every *mandatory* header (a header that changes the GRANDPA authority set) — the pallet documents this itself: "the pallet can't go further without importing this header." `try_enact_authority_change` — the function responsible for applying such a change — explicitly refuses to process any header containing a GRANDPA *forced* change, or a *scheduled* change with a non-zero delay, always returning `Error::<T, I>::UnsupportedScheduledChange`. Since GRANDPA's own consensus protocol can legitimately emit forced changes (e.g. as a stall-recovery mechanism when the current validator set fails to finalize blocks in time), this is not an attacker-crafted edge case but a real, protocol-level scenario the pallet cannot handle. Once it occurs on the bridged chain, `submit_finality_proof_ex` will permanently reject that header and every subsequent header (since the authority set can never advance), freezing the light client — and by extension the parachains/messages pallets built on top of it — exactly like the Chainlink aggregator getting stuck in the original report.

### Finding Description
The relevant logic is in `try_enact_authority_change`: [1](#0-0) 

- `find_forced_change` unconditionally rejects a header if it contains a forced authority-set change.
- `find_scheduled_change` accepts a scheduled change only if `delay == Zero::zero()`; any other delay causes the same `UnsupportedScheduledChange` error.

This is invoked from the pallet's sole public entrypoint, `submit_finality_proof_ex`: [2](#0-1) 

The pallet's own documentation states that mandatory (authority-set-changing) headers are not optional — the light client cannot progress without importing them: [3](#0-2) 

If the bridged chain ever produces a mandatory header whose consensus digest is a forced change (or a delayed scheduled change), `submit_finality_proof_ex` will revert with `UnsupportedScheduledChange` for that header, and for every later header signed by the new set, forever. There is no unprivileged path to recover: the check has no bypass and no alternate acceptance route. The pallet's `README.md` acknowledges this directly: "We don't support forced changes - at that point governance intervention is required." — i.e. the only path forward is a privileged root/governance/owner call.

This is the structural analog of the seed bug: like `TwapOracle.consult()` reverting forever once a Chainlink aggregator gets stuck with no on-chain way to swap the aggregator, `submit_finality_proof_ex` reverts forever once the bridged GRANDPA set changes via a mechanism the verifier code doesn't support, with no on-chain way for ordinary relayers to unstick it.

### Impact Explanation
Once triggered, the light client permanently stops accepting new finalized headers from the bridged chain. Every pallet that depends on the GRANDPA pallet's stored best-finalized header for storage-proof verification — the bridge parachains pallet and, transitively, the bridge messages pallet used for XCM message delivery between chains (e.g. the Polkadot↔Kusama / BridgeHub bridges) — stalls indefinitely. Message delivery and delivery-confirmation proofs can no longer be verified, halting cross-chain message processing and any pending relayer rewards tied to confirmation. This matches "public underpriced work that degrades block production or stalls bridge processing" / "permanent... bridge-state lock" in the impact gate, since it is a chain-driven condition external to this runtime, not an admin/governance action, that produces the stall.

### Likelihood Explanation
Forced GRANDPA changes are a real, documented consensus-protocol feature meant to be used precisely when the finality gadget stalls (validators fail to finalize for an extended period) — i.e. they are most likely to occur exactly when the bridged chain is already struggling, which is also the moment a bridge needs to remain functional. The pallet was written to reject this case outright rather than degrade gracefully, so no relayer or user action is required to trigger it beyond the bridged chain's own consensus emitting such a change; no malicious peer, validator, or governance abuse is needed to enter the stuck state.

### Recommendation
Confirm current mitigation status: a later change added `force_set_pallet_state` (root/governance-only) to reset the pallet's authority set and best-finalized header without additional checks, mirroring the "governance can fix it, ideally with a timelock" mitigation recommended for the original Vader finding: [4](#0-3) 

Verify that every deployed bridge instance (BridgeHub Rococo/Westend, Polkadot/Kusama, etc.) has this call wired to a governance track with bounded latency (ideally with a timelock, as recommended in the original report), and add monitoring/alerting so operators are notified immediately once `UnsupportedScheduledChange` is emitted, since users have no way to detect or resolve the stall on their own.

### Proof of Concept
1. Deploy `pallet-bridge-grandpa` bridging to a source chain, with `CurrentAuthoritySet` at `set_id = N`.
2. On the bridged (source) chain, GRANDPA's stall-recovery logic (or any code path producing a `ScheduledChange` digest with `delay != 0`, or a `ForcedChange` digest) emits such a change in a finalized header — this is a legitimate GRANDPA behavior, not an on-chain exploit.
3. A relayer submits this header's finality proof via `submit_finality_proof_ex`.
4. `try_enact_authority_change` (bridges/modules/grandpa/src/lib.rs:611-642) detects the forced/delayed change and returns `Error::<T, I>::UnsupportedScheduledChange`; the call reverts.
5. Because this header is mandatory (it enacts the new authority set) and the pallet's stored `CurrentAuthoritySet` never advances, every subsequent header signed by the new authority set also fails `verify_justification` against the stale stored set — the pallet can never resynchronize.
6. Dependent pallets (bridge parachains pallet, bridge messages pallet) can no longer verify new storage proofs; cross-chain message delivery and confirmation stall until a privileged `force_set_pallet_state`/`initialize` call is issued by governance/root/owner.

### Citations

**File:** bridges/modules/grandpa/src/lib.rs (L301-313)
```rust
			// it checks whether the `number` is better than the current best block number
			// and whether the `current_set_id` matches the best known set id
			let improved_by =
				SubmitFinalityProofHelper::<T, I>::check_obsolete(number, Some(current_set_id))?;

			let authority_set = <CurrentAuthoritySet<T, I>>::get();
			let unused_proof_size = authority_set.unused_proof_size();
			let set_id = authority_set.set_id;
			let authority_set: AuthoritySet = authority_set.into();
			verify_justification::<T, I>(&justification, hash, number, authority_set)?;

			let maybe_new_authority_set =
				try_enact_authority_change::<T, I>(&finality_target, set_id)?;
```

**File:** bridges/modules/grandpa/src/lib.rs (L604-629)
```rust
	/// Check the given header for a GRANDPA scheduled authority set change. If a change
	/// is found it will be enacted immediately.
	///
	/// This function does not support forced changes, or scheduled changes with delays
	/// since these types of changes are indicative of abnormal behavior from GRANDPA.
	///
	/// Returned value will indicate if a change was enacted or not.
	pub(crate) fn try_enact_authority_change<T: Config<I>, I: 'static>(
		header: &BridgedHeader<T, I>,
		current_set_id: sp_consensus_grandpa::SetId,
	) -> Result<Option<AuthoritySet>, DispatchError> {
		// We don't support forced changes - at that point governance intervention is required.
		ensure!(
			GrandpaConsensusLogReader::<BridgedBlockNumber<T, I>>::find_forced_change(
				header.digest()
			)
			.is_none(),
			<Error<T, I>>::UnsupportedScheduledChange
		);

		if let Some(change) =
			GrandpaConsensusLogReader::<BridgedBlockNumber<T, I>>::find_scheduled_change(
				header.digest(),
			) {
			// GRANDPA only includes a `delay` for forced changes, so this isn't valid.
			ensure!(change.delay == Zero::zero(), <Error<T, I>>::UnsupportedScheduledChange);
```

**File:** bridges/modules/grandpa/README.md (L27-38)
```markdown
There are two main things in GRANDPA that help building light clients:

- there's no need to import all headers of the bridged chain. Light client may import finalized headers or just
  some of finalized headers that it consider useful. While the validators set stays the same, the client may
  import any header that is finalized by this set;

- when validators set changes, the GRANDPA gadget adds next set to the header. So light client doesn't need to
  verify storage proofs when this happens - it only needs to look at the header and see if it changes the set.
  Once set is changed, all following justifications are generated by the new set. Header that is changing the
  set is called "mandatory" in the pallet. As the name says, the light client need to import all such headers
  to be able to operate properly.

```

**File:** prdoc/1.13.0/pr_4465.prdoc (L4-17)
```text
title: "Bridge: added force_set_pallet_state call to pallet-bridge-grandpa"

doc:
  - audience: Runtime Dev
    description: |
      Added `force_set_pallet_state` to the `pallet-bridge-grandpa`. It is only callable by the
      root (governance or sudo) and may be used to update current authorities set and the best
      finalized header without any additional checks.

crates:
  - name: pallet-bridge-grandpa
    bump: major
  - name: bridge-hub-rococo-runtime
    bump: minor
```
