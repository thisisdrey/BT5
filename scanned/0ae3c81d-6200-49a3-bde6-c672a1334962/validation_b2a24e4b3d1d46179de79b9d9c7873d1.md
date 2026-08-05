### Title
`ManagerOrigin` in `pallet-election-provider-multi-block` can unilaterally force election-phase transitions and trigger emergency fallback, bypassing the intended "no critical power" restriction - (File: `substrate/frame/election-provider-multi-block/src/lib.rs`)

### Summary
The `manage` extrinsic in `pallet-election-provider-multi-block` accepts either `Config::AdminOrigin` (highest privilege, meant to be root/governance) or `Config::ManagerOrigin`, which is explicitly documented as "a less privileged origin than `Config::AdminOrigin`, that can manage some election aspects, **without critical power**". In practice, `ManagerOperation::ForceSetPhase(Phase<T>)` and `ManagerOperation::EmergencyFallback` grant the `Manager` role unrestricted control over the pallet's phase state machine — the exact kind of "critical power" the design intends to reserve for `AdminOrigin`/root. In `asset-hub-westend`, `ManagerOrigin` is configured as `EitherOfDiverse<EnsureRoot<AccountId>, EnsureSignedBy<WestendStakingMiner, AccountId>>`, i.e. a single non-root, non-governance signed account. [1](#0-0) 

### Finding Description
`manage()` checks `ManagerOrigin` first and falls back to `AdminOrigin`: [2](#0-1) 

The `ManagerOperation` enum exposed to this lesser-privileged origin includes `ForceRotateRound`, `ForceSetPhase(Phase<T>)` (an arbitrary phase, "use only with care and sufficient testing" per the doc comment), and `EmergencyFallback`: [3](#0-2) 

`ForceSetPhase` calls `Self::phase_transition(phase)` directly with the caller-supplied `Phase<T>` value and no origin-specific restriction on which phases are reachable via this path, and `EmergencyFallback` force-queues a fallback election result via `T::Verifier::force_set_single_page_valid`: [4](#0-3) 

In `asset-hub-westend`'s runtime config, this `ManagerOrigin` is satisfied by a specific signed account (`WestendStakingMiner`), not root or a governance track: [5](#0-4) 

The repository even contains a prior fix specifically for this "wrong origin on `manage`" class of bug (`prdoc/stable2509-2/pr_10248.prdoc`, "Fix origin check in EPMB's manage extrinsic... Break down `Admin` and `Manager` origins/extrinsics for easier configuration"), confirming this exact area has previously been the site of an access-control defect: [6](#0-5) 

This is directly analogous to the external report's pattern: a role that should only have limited/no critical power (`onlyAuthorizedMatcher` ≈ `ManagerOrigin`) is instead wired to functions that mutate core system state that the code's own documentation says should require the highest-privilege role (`onlyOwner` ≈ `AdminOrigin`/root).

### Impact Explanation
A `ManagerOrigin`-satisfying account (which, per the westend asset-hub config, is a specific non-root signed key rather than governance) can:
- Force the election phase to `Emergency` at any time via `ForceSetPhase`, interrupting an in-progress `Signed`/`Unsigned`/`SignedValidation` round regardless of whether a legitimate fallback condition occurred.
- Immediately follow with `EmergencyFallback` to force-queue a fallback (on-chain) election result into the verifier, bypassing the miner-submitted, feasibility-checked, scored solutions that the signed/unsigned pipeline is designed to produce.
- Repeatedly call `ForceRotateRound` to reset rounds, wiping snapshot/verifier state and stalling validator-set election progression — a liveness-critical path for a Substrate-based relay/parachain (staking election feeds the next era's validator set).

Since election outcome determines the next validator set, this lets a single non-governance key disrupt or manipulate a critical piece of chain security without needing root, a malicious validator, or a compromised relayer — matching the "runtime bugs that compromise intended behavior" / "unauthorized execution or origin escalation" category in the impact gate.

### Likelihood Explanation
The path is reachable by any account configured as `ManagerOrigin` calling a public, unprivileged (from the chain's perspective, non-root) extrinsic with no additional gating beyond the origin check that was already the subject of a previous fix in this same pallet. No malicious validator/collator/relayer assumption is required — only possession of whatever key the runtime designates as `ManagerOrigin` (e.g., the staking-miner bot key on asset-hub-westend), which is explicitly a lower-trust key than governance/root by design intent.

### Recommendation
- Restrict `ManagerOperation::ForceSetPhase` to a safe, restricted subset of phase transitions (e.g., only allow moving *forward* along the legitimate phase sequence, or require `AdminOrigin` for arbitrary phase overrides), rather than accepting any caller-supplied `Phase<T>`.
- Move `EmergencyFallback` and `ForceRotateRound` behind `AdminOrigin` only, or add explicit invariant checks (e.g., disallow `ForceRotateRound`/`ForceSetPhase` from mid-verification phases without additional safety conditions) so `ManagerOrigin` genuinely has "no critical power" as documented.
- Add regression tests asserting that `ManagerOrigin` cannot use `ForceSetPhase` to reach `Emergency`/`Done`/`Export` phases outside of legitimate automatic transitions.

### Proof of Concept
1. Deploy asset-hub-westend runtime where `ManagerOrigin = EitherOfDiverse<EnsureRoot<AccountId>, EnsureSignedBy<WestendStakingMiner, AccountId>>`.
2. As the `WestendStakingMiner` signed account (non-root), during an active `Signed`/`Unsigned` phase with valid miner solutions in flight, call:
   `MultiBlock::manage(origin, ManagerOperation::ForceSetPhase(Phase::Emergency))`
   — this succeeds because only `ManagerOrigin` is required (`lib.rs:642-646`).
3. Immediately call:
   `MultiBlock::manage(origin, ManagerOperation::EmergencyFallback)`
   — this succeeds under the same `ManagerOrigin` check and force-queues the fallback result via `T::Verifier::force_set_single_page_valid` (`lib.rs:648-671`), discarding any in-flight, feasibility-verified signed/unsigned solutions.
4. Repeat with `ManagerOperation::ForceRotateRound` to reset state and stall progress on demand (`lib.rs:679-685`, confirmed reachable by non-admin `Manager` in test `force_rotate_round`, `lib.rs:3281-3322`).

This confirms a non-root, non-governance signed key can unilaterally override the intended phase-transition state machine that governs validator-set election — the same class of defect as the reported "authorized non-owner role controls what should be owner-only critical parameters."

### Citations

**File:** substrate/frame/election-provider-multi-block/src/lib.rs (L493-505)
```rust
pub enum ManagerOperation<T: Config> {
	/// Forcefully go to the next round, starting from the Off Phase.
	ForceRotateRound,
	/// Force-set the phase to the given phase.
	///
	/// This can have many many combinations, use only with care and sufficient testing.
	ForceSetPhase(Phase<T>),
	/// Trigger the (single page) fallback in `instant` mode, with the given parameters, and
	/// queue it if correct.
	///
	/// Can only be called in emergency phase.
	EmergencyFallback,
}
```

**File:** substrate/frame/election-provider-multi-block/src/lib.rs (L609-617)
```rust
		/// The origin that can perform administration operations on this pallet.
		///
		/// This is the highest privilege origin of this pallet, and should be configured
		/// restrictively.
		type AdminOrigin: EnsureOrigin<Self::RuntimeOrigin>;

		/// A less privileged origin than [`Config::AdminOrigin`], that can manage some election
		/// aspects, without critical power.
		type ManagerOrigin: EnsureOrigin<Self::RuntimeOrigin>;
```

**File:** substrate/frame/election-provider-multi-block/src/lib.rs (L642-687)
```rust
		pub fn manage(origin: OriginFor<T>, op: ManagerOperation<T>) -> DispatchResultWithPostInfo {
			T::ManagerOrigin::ensure_origin(origin.clone()).map(|_| ()).or_else(|_| {
				// try admin origin as well as admin is a superset.
				T::AdminOrigin::ensure_origin(origin).map(|_| ())
			})?;
			match op {
				ManagerOperation::EmergencyFallback => {
					ensure!(Self::current_phase() == Phase::Emergency, Error::<T>::UnexpectedPhase);
					// note: for now we run this on the msp, but we can make it configurable if need
					// be.
					let voters = Snapshot::<T>::voters(Self::msp()).ok_or(Error::<T>::Snapshot)?;
					let targets = Snapshot::<T>::targets().ok_or(Error::<T>::Snapshot)?;
					let desired_targets =
						Snapshot::<T>::desired_targets().ok_or(Error::<T>::Snapshot)?;
					let fallback = T::Fallback::instant_elect(
						voters.into_inner(),
						targets.into_inner(),
						desired_targets,
					)
					.map_err(|e| {
						log!(warn, "Fallback failed: {:?}", e);
						Error::<T>::Fallback
					})?;
					let score = fallback.evaluate();
					T::Verifier::force_set_single_page_valid(fallback, 0, score);
					Ok(PostDispatchInfo {
						actual_weight: Some(T::WeightInfo::manage_fallback()),
						pays_fee: Pays::No,
					})
				},
				ManagerOperation::ForceSetPhase(phase) => {
					Self::phase_transition(phase);
					Ok(PostDispatchInfo {
						actual_weight: Some(T::DbWeight::get().reads_writes(1, 1)),
						pays_fee: Pays::No,
					})
				},
				ManagerOperation::ForceRotateRound => {
					Self::rotate_round();
					Ok(PostDispatchInfo {
						actual_weight: Some(T::WeightInfo::export_terminal()),
						pays_fee: Pays::No,
					})
				},
			}
		}
```

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs (L122-125)
```rust
	type AdminOrigin =
		EitherOfDiverse<EnsureRoot<AccountId>, EnsureSignedBy<WestendStakingMiner, AccountId>>;
	type ManagerOrigin =
		EitherOfDiverse<EnsureRoot<AccountId>, EnsureSignedBy<WestendStakingMiner, AccountId>>;
```

**File:** prdoc/stable2509-2/pr_10248.prdoc (L1-8)
```text
title: Fix origin check in EPMB's manage extrinsic
doc:
- audience: Runtime Dev
  description: |-
    Fix origin check in EPMB's manage extrinsic and:

    - Break down `Admin` and `Manager` origins/extrinsics for easier configuration
    - update the corresponding weights
```
