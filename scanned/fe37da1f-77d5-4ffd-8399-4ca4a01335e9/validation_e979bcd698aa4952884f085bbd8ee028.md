## Title
Reentrancy stipend protection is never applied to non-zero-value `.transfer()`/`.send()` calls due to match-arm ordering bug in EVM `CALL` handling - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

### Summary
`pallet-revive`'s EVM interpreter explicitly re-implements Solidity's classic 2300-gas call-stipend reentrancy guard for `.transfer()`/`.send()`, because — as the code itself documents — a caller cannot rely on Ethereum's raw gas-stipend value to prevent reentrancy on revive's PVM gas scale. The dedicated `ReentrancyProtection::AllowNext` mode was added for exactly this reason. However, the match statement selecting between `AllowReentry` and `AllowNext` is ordered so that **any non-zero-value call** short-circuits to `AllowReentry` before the stipend heuristic is ever evaluated, meaning the actual common case this protection was built for — `target.transfer(amount)` / `target.send(amount)` with `amount > 0` — never receives the `AllowNext` restriction. This defeats the reentrancy mitigation that is the direct local analog of the `UniERC20` `.call{value:X}` reentrancy risk described in the external report.

### Finding Description
In `run_call()`: [1](#0-0) 

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

Per `prdoc/stable2603/pr_11227.prdoc`, solc compiles `target.transfer(amount)`/`target.send(amount)` with `amount > 0` to pass `gas_limit = 0` on the stack and relies on the runtime to add the 2300 stipend and its associated protection. Because Rust `match` arms are evaluated top-to-bottom and the first arm `(false, _)` matches whenever `value.is_zero() == false` — irrespective of `gas_limit` — every non-zero-value call (the actual `.transfer()`/`.send()` path) is routed to `ReentrancyProtection::AllowReentry`, not `AllowNext`. The `(_, true) => AllowNext` arm is only reachable when `value.is_zero() == true`, i.e. for zero-value transfers, which is not the scenario the stipend exists to protect (there is nothing to steal when `amount == 0`).

`ReentrancyProtection::AllowReentry` does not restrict the callee from re-entering the caller's frame at all: [2](#0-1) 

Only `AllowNext` causes `self.top_frame_mut().allows_reentry = false;`, which is exactly the mechanism documented in `ReentrancyProtection`'s doc comment as the explicit replacement for gas-stipend-only protection: [3](#0-2) 

Since weight/gas metering scale differences between Ethereum and PVM are exactly why this reentrancy flag was introduced (per the code's own comments), the weight budget alone (`add_stipend = true`, i.e., `CallResources::from_ethereum_gas(gas_limit, true)`) cannot be trusted to stop a reentrant sub-call — and the flag-based guard that was supposed to compensate never activates for the value-transferring case.

### Impact Explanation
Any Solidity contract deployed on `pallet-revive` that follows the standard "checks-effects-interactions" pattern using `.transfer()`/`.send()` — relying on the documented 2300-gas stipend to prevent a malicious receiver from re-entering — is not actually protected on this chain. A malicious contract receiving native value via `.transfer()`/`.send()` can attempt to re-enter the sender in its `receive()`/fallback, and because `AllowReentry` is granted, the reentrancy-denial check that would otherwise block the callback (`allows_reentry(&dest)` / `<Error<T>>::ReentranceDenied`) is bypassed. This can enable classic reentrancy fund-draining or double-spend against unmodified, "safe" Solidity contracts that assumed the stipend model works as on Ethereum — a direct compromise of runtime/contract intended behavior and potential theft of contract-held value, matching the "theft or unbacked mint or unlock" / "runtime bugs that compromise intended behavior" impact classes.

### Likelihood Explanation
This is triggerable by any unprivileged user deploying and interacting with an ordinary Solidity contract compiled by solc and executed through `pallet-revive`'s EVM interpreter — no admin, governance, relayer, or validator involvement is required. Because `.transfer()`/`.send()` with non-zero value is an extremely common Solidity idiom (used precisely because developers believe it is reentrancy-safe), the affected code path is hit by default whenever value is moved this way, making exploitation straightforward for any attacker who controls the receiving contract.

### Recommendation
Reorder the match so the stipend/value-scale heuristic is evaluated before falling back to the generic non-zero-value case, e.g.:
```rust
let (add_stipend, reentracy) =
    match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND)) {
        (_, true) => (true, ReentrancyProtection::AllowNext),
        (false, _) => (true, ReentrancyProtection::AllowReentry),
        (_, _) => (false, ReentrancyProtection::AllowReentry),
    };
```
so that any call carrying the compiler's stipend signal (`gas_limit == CALL_STIPEND`) — regardless of whether `value` is zero or non-zero — receives `AllowNext`. Add regression tests (mirroring `testTransferReentrancy`/`testSendReentrancy` in `substrate/frame/revive/fixtures/contracts/Stipends.sol`) that specifically exercise non-zero-value `.transfer()`/`.send()` reentrancy attempts to confirm the guard actually engages.

### Proof of Concept
1. Deploy a `Victim` Solidity contract on `pallet-revive` implementing a withdraw pattern: `balances[msg.sender] -= amount; msg.sender.transfer(amount);` (or `.send(amount)`).
2. Deploy an `Attacker` contract whose `receive()`/`fallback()` calls back into `Victim.withdraw()` (or any state-mutating function) when it receives funds.
3. Attacker calls `Victim.withdraw()`. Solc compiles the `.transfer(amount)` with `amount > 0` and `gas_limit = 0` pushed on the stack.
4. In `run_call()`, `value.is_zero() == false` matches arm `(false, _)` first, yielding `ReentrancyProtection::AllowReentry` — the callee is not blocked from calling back into `Victim`'s frame.
5. `Attacker.receive()` re-enters `Victim.withdraw()` before `balances[msg.sender]` effects are otherwise finalized/observed by the outer frame, allowing repeated withdrawals within the stipend's weight budget (e.g., via a cheap plain-balance-transfer path rather than full EVM execution) — draining more funds than `Attacker`'s actual balance entitles it to, in contrast to the `testTransferReentrancy`/`testSendReentrancy` fixture tests in `Stipends.sol` which assume this exact scenario is blocked.

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

**File:** substrate/frame/revive/src/exec.rs (L134-141)
```rust
	/// Activate reentrancy protection where the direct callee can be the same contract as the
	/// caller but none of the recursive callees of the callee must be the caller.
	///
	/// This is used for calls that transfer value but restrict gas so that the callee only has a
	/// stipend gas amount. In Ethereum that is not sufficient for the callee to make another call.
	/// However, due to gas scale differences that guarantee does not automatically hold in revive
	/// and we enforce it explicitly here.
	AllowNext,
```

**File:** substrate/frame/revive/src/exec.rs (L2186-2192)
```rust
			if !self.allows_reentry(&dest) {
				return Err(<Error<T>>::ReentranceDenied.into());
			}

			if allows_reentry == ReentrancyProtection::AllowNext {
				self.top_frame_mut().allows_reentry = false;
			}
```
