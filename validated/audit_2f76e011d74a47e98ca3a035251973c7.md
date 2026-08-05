Audit Report

## Title
`run_call` in pallet-revive's EVM-compatibility layer grants `AllowReentry` (no reentrancy denial) to non-zero-value `.transfer()`/`.send()` calls that receive the same stipend weight budget as the zero-value case, which is protected by `AllowNext` - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

## Summary
`run_call` selects reentrancy protection via `match (value.is_zero(), gas_limit == CALL_STIPEND)`, and because Rust tuple matching evaluates arms top-down, every non-zero-value call unconditionally falls into `(false, _) => (true, AllowReentry)` regardless of `gas_limit`, while only the rare `transfer(0)`/`send(0)` case (where solc explicitly emits `gas_limit = 2300`) reaches `(_, true) => (true, AllowNext)`. [1](#0-0)  Both branches set `add_stipend = true`, meaning both cases receive the identical stipend-derived weight budget from `determine_call_stipend`, but only the zero-value case gets the explicit deny-reentry flag (`AllowNext`) that would prevent the callee from reentering the caller. [2](#0-1)  The project's own PRDoc confirms that the common `target.transfer(amount>0)`/`target.send(amount>0)` pattern compiles with `gas_limit = 0` (not `2300`), so it is the `(false, _)` arm — `AllowReentry` — that governs the realistic, security-relevant usage, while `AllowNext` only ever protects the near-unused zero-value edge case. [3](#0-2) 

## Finding Description
The reentrancy-denial mechanism is implemented via `ReentrancyProtection::Strict`/`AllowNext`, which is enforced in `Stack::call` by clearing `top_frame_mut().allows_reentry` before or after entering the child frame; `AllowReentry` (the default match value, not `Strict`/`AllowNext`) performs neither check, leaving the caller's frame reenterable. [4](#0-3)  Both the `AllowReentry` and `AllowNext` code paths add the same `add_stipend = true` weight allowance in `run_call`, so the only distinguishing safety control between the two paths is the reentrancy flag itself, not the resource budget. [1](#0-0)  Since `determine_call_stipend` derives its weight budget from a benchmarked `EVMGas`→`Weight` conversion plus a fixed event-deposit allowance, rather than a value that is provably too small to execute one nested call/host-function invocation in PolkaVM (the way 2300 real Ethereum gas is provably insufficient for a `CALL` opcode), the non-zero-value branch (the actual common `.transfer()`/`.send()` pattern) relies solely on this weight-sizing coincidence for reentrancy safety, unlike the zero-value branch which additionally gets the explicit `AllowNext` denial. This is a genuine internal inconsistency in the code's own security model as attested by the code comments and the PRDoc test description, which state the intent is to protect the stipend-only call pattern from reentrancy, yet the arm ordering only ever applies that protection to the case that is not the standard non-zero-value transfer.

## Impact Explanation
If `determine_call_stipend`'s weight budget happens to be sufficient to cover a nested `call`/`call_evm` host invocation in PolkaVM's cost model, any contract deployed to pallet-revive that relies on Solidity's `.transfer()`/`.send()` idiom as its sole reentrancy guard for value-bearing withdrawals is exposed to reentrant execution before state is finalized, enabling double-withdrawal/fund-drain of contract-held value. This falls under the "contracts or revive execution" pivot requiring that public wrappers/execution paths "must not undercharge nested execution" and that contract-held value must "settle exactly once to the rightful beneficiary and amount."

## Likelihood Explanation
The path is reachable by any unprivileged EOA or contract calling a public entrypoint (e.g., `withdraw()`) on a target contract that uses `.transfer()`/`.send()` internally — no privileged actor, validator, collator, or relayer compromise is required. Exploitability is conditional on whether `determine_call_stipend`'s weight-equivalent budget in the specific runtime's cost table is large enough to cover one nested call invocation, which I was not able to fully quantify from the code alone (it depends on `<EVMGas as Token<T>>::weight` and the runtime's benchmarked host-call weights, which were not directly inspectable in the available index). This makes the bug's *logic flaw* (wrong reentrancy-protection arm for the common case) certain and verifiable from the code, while the *practical exploitability* depends on implementation-specific weight parameters that would need empirical/benchmark verification (e.g., a unit test attempting reentrant `call_evm` within the stipend budget) to conclusively demonstrate fund loss.

## Recommendation
Decouple the reentrancy-protection decision from `value.is_zero()`. Any call that is granted a stipend-only budget (`add_stipend = true`), including the common non-zero-value `.transfer(amount)`/`.send(amount)` case where solc passes `gas_limit = 0` and the runtime treats it as an implicit stipend grant, should receive `ReentrancyProtection::AllowNext` (or `Strict`), not `AllowReentry`. Additionally, audit/benchmark `determine_call_stipend`'s weight to ensure it is provably insufficient to cover the fixed weight cost of one nested `call`/`call_evm` host-function invocation, mirroring the real-EVM guarantee that 2300 gas cannot execute another `CALL`.

## Proof of Concept
1. Deploy a `VictimVault` contract with `withdraw()` that decrements caller balance then calls `to.transfer(amount)` (amount > 0).
2. Deploy an `Attacker` contract whose fallback/`receive()` calls back into `VictimVault.withdraw()`.
3. Attacker calls `withdraw()`; the internal `to.transfer(amount)` compiles to a `CALL` with `value > 0`, `gas_limit = 0`, hitting the `(false, _) => (true, AllowReentry)` arm in `run_call` at `substrate/frame/revive/src/vm/evm/instructions/contract.rs:193-203`.
4. Because `AllowReentry` performs no `top_frame_mut().allows_reentry = false` update (unlike `Strict`/`AllowNext` in `substrate/frame/revive/src/exec.rs:2160-2192`), Attacker's `receive()` can attempt to reenter `VictimVault.withdraw()` within the same stipend weight budget.
5. A Rust integration test (e.g., in `substrate/frame/revive/src/exec/tests.rs`, following the existing `evm_call_stipends_work_for_transfer_zero`/`evm_call_stipends_work_for_send_zero` pattern) should be added for the non-zero-value case, asserting whether reentry is denied or allowed, and if a nested `call_evm` succeeds within the stipend budget, demonstrating double-spend of vault funds.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L193-203)
```rust
	let (add_stipend, reentracy) =
		match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
		{
			(false, _) => (true, ReentrancyProtection::AllowReentry),
			// Heuristic: detect when solc passes `gas_limit = 2300` (the call stipend).
			// For zero-value transfer/send, solc injects `gas_limit = 2300` explicitly.
			// We apply `AllowNext` reentrancy protection and set `add_stipend = true` since the
			// raw 2300 gas value is only meaningful at Ethereum's gas scale.
			(_, true) => (true, ReentrancyProtection::AllowNext),
			(_, _) => (false, ReentrancyProtection::AllowReentry),
		};
```

**File:** substrate/frame/revive/src/metering/math.rs (L32-39)
```rust
fn determine_call_stipend<T: Config>() -> Weight {
	let gas_weight = <EVMGas as Token<T>>::weight(&EVMGas(CALL_STIPEND));
	let event_weight = <RuntimeCosts as Token<T>>::weight(&RuntimeCosts::DepositEvent {
		num_topic: STIPEND_LOG_TOPICS,
		len: STIPEND_LOG_DATA_LEN,
	});
	gas_weight.saturating_add(event_weight)
}
```

**File:** prdoc/stable2603/pr_11227.prdoc (L1-19)
```text
title: 'pallet-revive: add zero-value transfer/send stipend tests'
doc:
- audience: Runtime Dev
  description: "## Summary\n\nAdd tests that verify the `AllowNext` reentrancy path\
    \ is triggered for zero-value `transfer` and `send` calls.\n\n### How solc 0.8.30\
    \ handles the 2300 gas stipend\n\n| Solidity call | value | gas passed by compiler\
    \ | Stipend source |\n|---|---|---|---|\n| `target.transfer(amount)` | > 0 | `0`\
    \ | EVM adds 2300 automatically |\n| `target.send(amount)` | > 0 | `0` | EVM adds\
    \ 2300 automatically |\n| `target.transfer(0)` | 0 | `2300` | Compiler injects\
    \ explicitly |\n| `target.send(0)` | 0 | `2300` | Compiler injects explicitly\
    \ |\n| `target.call{value: v}(\"\")` | any | remaining gas | No stipend (forwards\
    \ all gas) |\n\nThe zero-value case is the one detected by our `gas_limit == CALL_STIPEND`\
    \ heuristic, which triggers `AllowNext`.\n\n## Changes\n\n- Add `testTransferZero`\
    \ / `testSendZero` to `Stipends.sol` fixture \u2014 these call `transfer(0)` and\
    \ `send(0)` on EOA, DoNothingReceiver, and SimpleReceiver\n- Add corresponding\
    \ Rust tests that exercise the `AllowNext` path\n- Add trace logs to the call\
    \ stipend match for debugging\n\n## Test plan\n\n- [x] `evm_call_stipends_work_for_transfer_zero`\
    \ passes, logs show `gas_limit=2300` \u2192 `AllowNext`\n- [x] `evm_call_stipends_work_for_send_zero`\
    \ passes, logs show `gas_limit=2300` \u2192 `AllowNext`"
```

**File:** substrate/frame/revive/src/exec.rs (L2160-2192)
```rust
		allows_reentry: ReentrancyProtection,
		read_only: bool,
	) -> Result<(), ExecError> {
		// Before pushing the new frame: Protect the caller contract against reentrancy attacks.
		// It is important to do this before calling `allows_reentry` so that a direct recursion
		// is caught by it.

		if allows_reentry == ReentrancyProtection::Strict {
			self.top_frame_mut().allows_reentry = false;
		}

		// We reset the return data now, so it is cleared out even if no new frame was executed.
		// This is for example the case for balance transfers or when creating the frame fails.
		*self.last_frame_output_mut() = Default::default();

		let try_call = || {
			// Enable read-only access if requested; cannot disable it if already set.
			let is_read_only = read_only || self.is_read_only();

			// We can skip the stateful lookup for pre-compiles.
			let dest = if <AllPrecompiles<T>>::get::<Self>(dest_addr.as_fixed_bytes()).is_some() {
				T::AddressMapper::to_fallback_account_id(dest_addr)
			} else {
				T::AddressMapper::to_account_id(dest_addr)
			};

			if !self.allows_reentry(&dest) {
				return Err(<Error<T>>::ReentranceDenied.into());
			}

			if allows_reentry == ReentrancyProtection::AllowNext {
				self.top_frame_mut().allows_reentry = false;
			}
```
