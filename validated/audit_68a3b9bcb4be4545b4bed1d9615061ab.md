Audit Report

## Title
EVM `CALL` reentrancy-protection selection defeats the "use `.transfer()` not `.call()`" safety pattern for non-zero-value transfers - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

## Summary
In `run_call`, the tuple match that decides whether to grant `ReentrancyProtection::AllowNext` (the mechanism designed to preserve Ethereum's 2300-gas-stipend reentrancy guarantee under revive's weight-based gas scale) checks `value.is_zero()` before checking `gas_limit == CALL_STIPEND`. Because `(false, _) => AllowReentry` is the first arm, any non-zero-value call — exactly the shape produced by the common `target.transfer(amount)`/`target.send(amount)` idiom with `amount > 0` — is routed to `AllowReentry` (no reentrancy protection) instead of `AllowNext`, even though `add_stipend` is still correctly set to `true` for that case.

## Finding Description
The match is: [1](#0-0) 

Per the project's own documentation of solc's gas-injection behavior, `target.transfer(amount)`/`target.send(amount)` with `amount > 0` cause solc to pass `gas_limit = 0` on the stack (the EVM protocol then implicitly grants the 2300 stipend), while only the *zero-value* variant (`transfer(0)`/`send(0)`) causes solc to explicitly push `gas_limit = 2300`: [2](#0-1) . That same prdoc's own title and description state plainly that the `AllowNext` path is exercised and tested only for the *zero-value* `transfer`/`send` case, not the non-zero-value case: [3](#0-2) .

`ReentrancyProtection::AllowNext` is documented in `exec.rs` as existing specifically to compensate for the fact that revive's gas/weight scale does not automatically preserve Ethereum's "2300 gas isn't enough for the callee to re-enter" guarantee: [4](#0-3) . Since `(false, _)` matches first regardless of the second tuple element, the real, common non-zero-value `.transfer()`/`.send()` call (`value.is_zero() == false`, `gas_limit == 0`) never reaches the `(_, true) => AllowNext` arm and instead receives `AllowReentry`, silently dropping the compensating protection for exactly the call pattern that the guard was purportedly built for.

## Impact Explanation
This affects any Solidity contract compiled by solc and deployed on `pallet-revive` that follows the standard, externally-recommended reentrancy mitigation of paying value via `.transfer()`/`.send()` instead of raw `.call()`. On revive, that call is granted `ReentrancyProtection::AllowReentry`, meaning the callee's fallback is free to re-enter the caller during the nested call, and — unlike genuine Ethereum, where 2300 raw gas categorically forbids `SSTORE` + `CALL` — there is no independent guarantee that the stipend-equivalent weight budget on revive is too small to complete a state-changing reentrant call. This is a runtime bug that defeats a widely-relied-upon safety idiom and can enable classic reentrancy fund-drain patterns (e.g., a payout contract calling `to.transfer(amount)` before finalizing internal accounting), reachable via ordinary public contract calls with no privileged actor involved.

## Likelihood Explanation
The triggering condition — a non-zero-value `CALL` with `gas_limit == 0` on the EVM stack — is the default code emitted by solc for `.transfer(amount)`/`.send(amount)` with `amount > 0`, the single most common Solidity pattern for sending value defensively. Reaching it requires only deploying two ordinary contracts and invoking a public function; no governance, validator, relayer, or off-chain privilege is needed. The `AllowNext` branch, by contrast, is confirmed by the project's own PR documentation to be exercised only for the less common zero-value transfer/send case, so the gap is not a hypothetical edge case but the default real-world code path.

## Recommendation
Decouple the `AllowNext` decision from `value.is_zero()`. Grant `AllowNext` whenever the effective gas budget for the callee is the stipend amount (i.e., whenever `add_stipend` is true and no additional explicit gas beyond the stipend was requested), regardless of whether `value` is zero or non-zero. Concretely, restructure the match/logic so that any call where the requested `gas_limit` is `0` (implicit-stipend case, non-zero value) or exactly `CALL_STIPEND` (explicit-stipend case, historically zero-value) receives `AllowNext`, and reserve `AllowReentry` only for calls that explicitly forward gas beyond the stipend.

## Proof of Concept
1. Deploy a `Victim` contract that decrements an internal balance mapping and then calls `to.transfer(amount)` with `amount > 0` to pay out — the standard reentrancy-safe idiom.
2. Deploy an `Attacker` contract whose `receive()` fallback makes a state-changing reentrant call back into `Victim` (e.g., `withdraw()` again), sized to fit within the stipend-equivalent weight budget.
3. Call `Victim.withdraw()` from `Attacker`. Because `run_call`'s match evaluates `(value.is_zero(), gas_limit == CALL_STIPEND)` and hits `(false, _) => AllowReentry` first (since `value` is the non-zero payout amount and `gas_limit` is `0` as emitted by solc), the nested call executes with no reentrancy protection.
4. Observe that `Attacker`'s fallback can re-enter `Victim.withdraw()` before the balance decrement is externally visible/finalized, extracting more funds than owed — confirming the `AllowNext` branch, and its underlying invariant, is bypassed for the real non-zero-value `.transfer()` path. This can be run as a Rust integration test analogous to the existing `evm_call_stipends_work_for_transfer_zero`/`evm_call_stipends_work_for_send_zero` tests in `substrate/frame/revive/src/exec/tests.rs`, but using a non-zero transfer amount to show `AllowReentry` is selected instead of `AllowNext`.

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
