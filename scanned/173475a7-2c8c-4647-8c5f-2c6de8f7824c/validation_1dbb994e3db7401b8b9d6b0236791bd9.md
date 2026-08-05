## Analysis

The zAuction report's core broken invariant: **a contract reference (which code/logic governs value-bearing operations) is rebound in storage instantly, but "old" and "new" execution contexts can both remain live and process actions against the same underlying state in the same window**, with no atomic transition or drain of in-flight processing tied to the old reference.

The direct, unprivileged (non-governance) local analog is `Ext::set_code_hash` in `pallet-revive`/`pallet-contracts`, exposed via the `set_code_hash` host function. Unlike the root-gated `Pallet::set_code` dispatchable, this is callable by **any contract's own Wasm/PVM code at will**, with no origin check — it rewrites `ContractInfo.code_hash` in storage immediately.### Title
`set_code_hash` lets a contract instantly rebind its own code reference mid-call, letting old-code and new-code logic run against the same state within one transaction - ([File: substrate/frame/contracts/src/exec.rs])

### Summary
The zAuction report's core defect is: a value-adjacent reference (the active auction contract) is rebound in storage *instantly and unconditionally*, with no mechanism to drain or invalidate in-flight processing under the old reference — producing a window where old and new logic can both accept/process operations against the same underlying state. `pallet-contracts`/`pallet-revive`'s `Ext::set_code_hash` (`substrate/frame/contracts/src/exec.rs:1583-1611`, host function documented at `substrate/frame/contracts/uapi/src/host.rs:660-688`) reproduces this exact class of defect, but crucially it is **not gated by root/governance** — any contract can call it on itself at will, unlike `Contracts::set_code` which requires `ensure_root` (`substrate/frame/contracts/src/lib.rs:892-918`).

### Finding Description
`set_code_hash` mutates `ContractInfo.code_hash` in the top execution frame immediately: [1](#0-0) 

This is exposed as an ordinary, unprivileged host function that any deployed contract's own code can invoke on itself, with no origin check comparable to `ensure_root` — contrast with the privileged `set_code` dispatchable which is explicitly root-only. The host function's own documentation confirms the exact "two versions active in parallel" hazard: [2](#0-1) 

Concretely: once a contract calls `set_code_hash(new_hash)`, `CodeHash<T>` for that account is updated in storage before the current call frame finishes executing. Any **subsequent, distinct** call into that address made from within the *same block* (e.g., a nested call from another contract, or a reentrant call routed differently than expected) will resolve to the new code and its semantics, while the *currently executing* call frame keeps running with the bytecode/instructions it already loaded for the old code, and any pending storage/deposit effects from that in-progress old-code execution are only reverted if the outer call itself later panics/reverts — otherwise they are committed. This mirrors the zAuction defect precisely: the "reference" switch is immediate and irreversible from the perspective of external observers/relayers, but the previously-referenced logic keeps executing and settling state for the remainder of the transaction, so old-code and new-code paths can both process value-relevant operations (e.g., differing access-control checks, differing payout logic, differing storage layout assumptions) against the same account in an overlapping window.

Unlike the zAuction case (an external EOA/admin switching a reference between two separately deployed contracts), this analog is triggerable entirely by the contract's own unprivileged logic, and by any second caller who races a call into the same address during the transition — no admin, governance, relayer, or validator is needed to create the parallel-version window.

### Impact Explanation
Falls under "runtime bugs that compromise intended behavior" and "unauthorized execution or origin escalation" categories under the Pivots: a contract that uses `set_code_hash` as a self-upgrade/proxy mechanism (a common pattern) can have its state processed by logic other than what it currently exposes as its canonical code, because the storage layout and any code-hash-dependent guards (access control, reentrancy limits, balance checks) are not guaranteed compatible across the instantaneous swap (`uapi/src/host.rs:667-669`). This can manifest as: value being moved/settled by stale in-flight logic that a reentrant/sibling call, executing under the newly bound code, did not anticipate — i.e. duplicate settlement, wrong beneficiary, or fund mishandling analogous to the "two auction contracts active in parallel" scenario in the original report.

### Likelihood Explanation
Moderate-to-low. Exploitability depends on a specific contract's own logic invoking `set_code_hash` mid-execution and being reentered or raced by a second caller before finishing — this requires an application-level proxy/upgrade pattern built on this primitive, not a flaw purely in the pallet itself. The pallet's documentation already flags the hazard explicitly, indicating it is a known, documented sharp edge rather than a silently broken invariant; whether any current runtime-deployed contract actually relies on `set_code_hash` in a way that creates an unrecoverable dual-version settlement window would require further investigation of consumer contracts, which is out of scope of this index.

### Recommendation
Treat `set_code_hash` as requiring the same "avoid keeping multiple versions active" discipline recommended in the zAuction report: contracts using it for upgrades should ensure the call frame that performs the swap does not continue further state-mutating operations afterward, and should guard against reentrancy into the same address during the swap (e.g. via `CallFlags`/reentrancy protections) so that no operation can complete under stale in-progress old-code semantics after the reference has already flipped in storage.

### Proof of Concept
Not independently reproducible from the index alone — this is a design-level hazard documented in the host function's own doc comment (`uapi/src/host.rs:675-678`) and confirmed by the implementation swapping `info.code_hash` synchronously mid-frame (`exec.rs:1591-1592`). Confirming exploitability against a real deployed contract that uses `set_code_hash` as an upgrade mechanism would require inspecting that contract's specific reentrancy/guard logic, which is not available in this repository's runtime pallet code alone.

### Citations

**File:** substrate/frame/contracts/src/exec.rs (L1583-1592)
```rust
	fn set_code_hash(&mut self, hash: CodeHash<Self::T>) -> DispatchResult {
		let frame = top_frame_mut!(self);
		if !E::from_storage(hash, &mut frame.nested_gas)?.is_deterministic() {
			return Err(<Error<T>>::Indeterministic.into());
		}

		let info = frame.contract_info();

		let prev_hash = info.code_hash;
		info.code_hash = hash;
```

**File:** substrate/frame/contracts/uapi/src/host.rs (L675-678)
```rust
	/// 3. If a contract calls into itself after changing its code the new call would use
	/// the new code. However, if the original caller panics after returning from the sub call it
	/// would revert the changes made by [`set_code_hash()`][`Self::set_code_hash`] and the next
	/// caller would use the old code.
```
