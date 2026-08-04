## Finding

### Title
Untrusted callee can immediately drain and schedule-destroy its caller's contract via the `ISystem` terminate-caller precompile path, corrupting caller state mid-call - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
Like the Hyperdrive `checkpoint` bug — where an unguarded state-mutating function could be re-entered mid-call to change reserves that another function had already assumed were stable, breaking a solvency check that runs *after* the mutating call — `pallet-revive`'s `Ext::terminate_caller` lets a callee contract instantly seize and schedule for deletion an **ancestor** frame's contract, while that ancestor is still on the call stack and has no reentrancy guard against this specific mutation.

### Finding Description
`Stack::terminate_caller` [1](#0-0)  walks up to the **parent** frame (`self.frames_mut().nth(1)`), reads its `ContractInfo`, and:
1. Immediately transfers the parent contract's entire EVM balance to a beneficiary chosen by the currently executing (callee) contract: [2](#0-1) 
2. Schedules the parent for deletion at the end of the transaction (delayed deletion, not immediate) by inserting into `contracts_to_be_destroyed`: [3](#0-2) 

The only guards present are that the *current* frame must not itself be a delegate-call frame, and the *parent* must not itself be a delegate-call frame, and the parent's `entry_point` must be `Call` (not `Constructor`): [4](#0-3) 

Crucially, unlike the same-contract `SELFDESTRUCT` path (`terminate_if_same_tx`), there is **no check that the parent is not "recursive"/still mid-execution** — analogous to `pallet-contracts`' older `Frame::terminate`, which explicitly rejected termination while the contract `is_recursive()` (`TerminatedWhileReentrant`) [5](#0-4) . `terminate_caller` deliberately targets a live ancestor on the current call stack — that is its entire purpose — with the deletion being merely delayed to end-of-transaction (`TerminateArgs { only_if_same_tx: false, .. }`), while the **balance transfer out of the parent happens immediately, synchronously, in the middle of the parent's own execution**.

This is the direct structural analog of the Hyperdrive report: a public-reachable code path (`ISystem`/`SELFDESTRUCT`-family precompile, reachable by any contract a caller calls into, e.g. via a low-level `call` in a swap/payout/refund routine) mutates critical shared state (the caller's own balance and pending-destruction status) mid-transaction, and the caller resumes execution afterward — its call to the untrusted contract returns normally (no automatic revert) — under the false assumption that its own balance and continued existence are unaffected. Any balance-dependent invariant the parent contract checks *before* making the external call (e.g. "I hold X, therefore I can safely promise Y to someone else after this call returns") is falsified by the parent's own balance having been drained underneath it during the call, exactly as `checkpoint()`'s unguarded state mutation falsified Hyperdrive's post-`_deposit` solvency assumption.

### Impact Explanation
Any contract on Asset Hub (or any chain enabling `pallet-revive`) that performs an external call to attacker-influenced code as part of a multi-step operation (e.g. a refund, a hook, a token callback, a DEX/AMM-style contract computing outputs based on its own balance before and after an external call) can have its entire native balance and storage/code destroyed mid-transaction by the callee, with the parent unaware that this happened. Combined with any subsequent logic in the parent contract that relies on its balance or existence (transfer-out based on `address(this).balance`, invariant checks, escrow release), this enables theft/loss of contract-held funds and violation of solvency/accounting invariants — squarely matching the "theft or unbacked mint or unlock" / "permanent user-fund lock" impact categories.

### Likelihood Explanation
This requires only an unprivileged attacker deploying a malicious contract that gets called (directly or transitively) by a victim contract during normal operation — no validator, relayer, governance, or node compromise is needed. The precondition is a victim contract making an external call to attacker-controlled code and trusting post-call balance/state continuity, a very common pattern (refund hooks, ERC-777/callback-style tokens, flash-loan-like flows). Reachability of `terminate_caller` itself is confirmed structurally in `exec.rs` and wired through `precompiles/builtin/system.rs`, but the exact Solidity-level call convention/selector that reaches `terminate_caller` (as opposed to `terminate_if_same_tx`) was not fully traced in this pass — this is the main open uncertainty.

### Recommendation
Add a reentrancy/liveness guard to `terminate_caller` analogous to `is_recursive()` in `pallet-contracts`: refuse to terminate an ancestor frame while any of its own logic remains to execute after the current callee returns (i.e., require the ancestor to itself be the frame directly initiating this call and about to unwind, or disallow the capability entirely for frames that will resume execution). At minimum, defer the balance transfer to end-of-transaction alongside the scheduled deletion (matching `only_if_same_tx: true` semantics) rather than executing it synchronously mid-stack, so a parent contract cannot observe a "call returned successfully" state while its balance has already vanished underneath it.

### Proof of Concept
Conceptual reproduction using existing test fixtures (`substrate/frame/revive/fixtures/contracts/Terminate.sol`, `TerminateCaller.sol`) as a base:
1. Deploy `Victim` contract with balance `B` that performs, in one public function: `(1) require(address(this).balance >= B, "solvent")`, `(2) low-level call into attacker-controlled `Callee`, `(3) transfer(promised_amount)` based on the balance check done in step 1.
2. `Callee`, invoked in step 2, calls the `ISystem` terminate-caller precompile path exercised by `Stack::terminate_caller` [1](#0-0) , naming itself (or a colluding address) as beneficiary of `Victim`'s entire balance.
3. Execution returns to `Victim` normally (no revert); `Victim` proceeds to step 3 and attempts `transfer(promised_amount)` against a balance that is now zero or attempts other balance-dependent logic against stale state, producing an invariant violation (failed/incorrect payout, or a subsequent unrelated caller griefed because `Victim`'s solvency assumption from step 1 no longer holds).

**Unverified/uncertain**: the exact external call convention (direct call vs. via `ISystem.sol`'s `terminate` selector) that a Solidity contract must use to reach `Ext::terminate_caller` specifically (as opposed to the self-`SELFDESTRUCT`/`terminate_if_same_tx` path) was not conclusively traced to `precompiles/builtin/system.rs` in this pass due to tool-call limits; a Devin session with full repo access should confirm the precompile dispatch table entry and selector before treating this as fully proven end-to-end.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L2550-2580)
```rust
	fn terminate_caller(&mut self, beneficiary: &H160) -> Result<(), DispatchError> {
		ensure!(self.top_frame().delegate.is_none(), Error::<T>::PrecompileDelegateDenied);
		let parent = self.frames_mut().nth(1).ok_or_else(|| Error::<T>::ContractNotFound)?;
		ensure!(parent.entry_point == ExportedFunction::Call, Error::<T>::TerminatedInConstructor);
		ensure!(parent.delegate.is_none(), Error::<T>::PrecompileDelegateDenied);

		let info = parent.contract_info();
		let trie_id = info.trie_id.clone();
		let code_hash = info.code_hash;
		let contract_address = T::AddressMapper::to_address(&parent.account_id);
		let beneficiary = T::AddressMapper::to_account_id(beneficiary);

		let parent_account_id = parent.account_id.clone();

		// balance transfer is immediate
		Self::transfer(
			&self.origin,
			&parent_account_id,
			&beneficiary,
			<Contracts<T>>::evm_balance(&contract_address),
			Preservation::Preserve,
			&mut top_frame_mut!(self).frame_meter,
			&self.exec_config,
		)?;

		// schedule for delayed deletion
		let args = TerminateArgs { beneficiary, trie_id, code_hash, only_if_same_tx: false };
		self.top_frame_mut().contracts_to_be_destroyed.insert(parent_account_id, args);

		Ok(())
	}
```

**File:** substrate/frame/contracts/src/exec.rs (L1363-1366)
```rust
	fn terminate(&mut self, beneficiary: &AccountIdOf<Self::T>) -> DispatchResult {
		if self.is_recursive() {
			return Err(Error::<T>::TerminatedWhileReentrant.into());
		}
```
