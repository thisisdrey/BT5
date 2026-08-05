## Title
EVM `CALL` reentrancy-protection selection defeats the "use `.transfer()` not `.call()`" safety pattern for non-zero-value transfers - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

### Summary
The external report recommends `.transfer()` (2300-gas stipend) over `.call()` to prevent reentrancy. `pallet-revive`'s EVM compatibility layer has a dedicated mechanism, `ReentrancyProtection::AllowNext`, built specifically to preserve that Ethereum guarantee on revive, because (per the code's own comment) revive's weight-based gas scale does not automatically make a 2300-gas stipend insufficient for a nested call the way it is on real Ethereum. However, the `match` in `run_call` that selects this protection is ordered so that **any non-zero-value call** — which is exactly the real-world shape of `target.transfer(amount)` / `target.send(amount)` — falls into the first arm and is granted `ReentrancyProtection::AllowReentry` (no protection at all), never reaching the `AllowNext` arm meant to guard it.

### Finding Description
In `run_call`: [1](#0-0) 

```rust
let (add_stipend, reentracy) =
    match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
    {
        (false, _) => (true, ReentrancyProtection::AllowReentry),
        // Heuristic: detect when solc passes `gas_limit = 2300` (the call stipend)...
        (_, true) => (true, ReentrancyProtection::AllowNext),
        (_, _) => (false, ReentrancyProtection::AllowReentry),
    };
```

Per the project's own documentation of how `solc` emits these calls (`prdoc/stable2603/pr_11227.prdoc`): [2](#0-1) 

- `target.transfer(amount)` with `amount > 0` → compiler passes `gas = 0` on the stack; the EVM's own protocol rule then implicitly grants the 2300-gas stipend.
- `target.transfer(0)` → compiler explicitly passes `gas = 2300`.

Because of Rust `match` arm ordering, `(value.is_zero() == false, _)` is checked **before** `(_, gas_limit == 2300)`. For the real, non-zero-value `.transfer()`/`.send()` case — where `gas_limit` on the stack is `0`, not `2300` — the tuple `(false, false)` still matches the very first arm, setting `add_stipend = true` (correctly recognizing the stipend is due) but `reentracy = ReentrancyProtection::AllowReentry` (incorrectly disabling reentrancy protection). The second arm, `(_, true) => AllowNext`, which is documented as the mechanism enforcing revive's stipend-based reentrancy guarantee, is unreachable for any non-zero value call regardless of the requested gas.

`ReentrancyProtection::AllowNext` is explicitly documented as existing because Ethereum's assumption ("2300 gas is not enough for the callee to make another call") does not automatically hold under revive's gas/weight scale: [3](#0-2) 

By routing the non-zero-value transfer case to `AllowReentry` instead, `pallet-revive`'s EVM layer silently drops the very protection it built to compensate for that gap — for exactly the call pattern (`.transfer(amount)`/`.send(amount)` with `amount > 0`) that the external report identifies as the "safe" pattern.

### Impact Explanation
Any Solidity contract deployed on `pallet-revive` (e.g. Asset Hub's EVM-compatibility pallet) that follows the textbook reentrancy mitigation — paying out non-zero value via `.transfer()`/`.send()` instead of `.call()` — does **not** actually get the intended reentrancy guard on this chain. The receiving contract's fallback is not blocked from re-entering the caller, and unlike genuine Ethereum, there is no independent guarantee that the granted (2300-gas-equivalent) weight budget is too small to complete a state-changing nested call, since the code comment itself states this equivalence does not automatically hold in revive's weight-based accounting. This can enable the classic reentrancy fund-drain pattern (e.g. a payout/vault contract making per-user payouts before finalizing internal accounting) directly through a public, unprivileged execution path — no admin, governance, relayer, or validator involvement required, matching the "runtime bug that compromises intended behavior" / "theft or unbacked... unlock" impact classes.

### Likelihood Explanation
High reachability: this triggers on the default, most common Solidity idiom for sending value (`.transfer(amount)` with `amount > 0`), executed through the standard `CALL` opcode path in `substrate/frame/revive/src/vm/evm/instructions/contract.rs`, reachable by any account calling any deployed EVM contract on a chain with `pallet-revive` configured (`prdoc/pr_12267.prdoc` notes it is enabled at pallet index 60 on Asset Hub Westend). No special privileges, malicious validators, or off-chain actors are needed — only a contract author (attacker-deployed) whose fallback performs a reentrant call within the granted stipend-equivalent weight, and a victim contract following the "use `.transfer()`" recommendation to pay it out.

### Recommendation
Reorder the match so the 2300-gas-equivalent stipend detection is evaluated independently of `value.is_zero()`, i.e., select `AllowNext` whenever the runtime determines the stipend must be granted (covering both the "`gas_limit == 2300`, value zero" solc-injected case and the "value != 0, gas_limit == 0" implicit-stipend case), and reserve `AllowReentry` only for calls that explicitly request gas beyond the stipend. Concretely, base the branch on whether `add_stipend` is the *only* gas being granted (i.e., requested explicit gas is `0`), not purely on `value.is_zero()`.

### Proof of Concept
1. Deploy a `Victim` contract on a `pallet-revive`-enabled chain that pays users via `to.transfer(amount)` after decrementing an internal balance mapping, following the common (and externally-recommended) reentrancy-safe pattern.
2. Deploy an `Attacker` contract whose `receive()`/fallback performs a state-changing nested call back into `Victim` (e.g., calling `withdraw()` again) that fits within the stipend-equivalent weight budget available for `AllowReentry` (no protection is engaged at all, so any such nested call that fits weight-wise succeeds, unlike genuine Ethereum's 2300 raw gas which categorically forbids `SSTORE`+`CALL`).
3. Trigger `Victim.withdraw()` from `Attacker`. Because `run_call` assigns `ReentrancyProtection::AllowReentry` (not `AllowNext`) for this non-zero-value, zero-explicit-gas call, `Attacker`'s fallback can re-enter `Victim` before its state update settles, draining funds — exactly the scenario the `AllowNext` mechanism (and the accompanying `ReentrancyAttacker`/`testTransferReentrancy` test in `substrate/frame/revive/fixtures/contracts/Stipends.sol`) was built to prevent, but which the match-arm ordering bypasses for the real (non-zero-value) `.transfer()` path.

Note: I could not execute this against a running node to confirm exploitability empirically (no runtime access in this environment); the analysis is based on static review of `run_call`'s match logic against the project's own documented solc gas-injection semantics (`pr_11227.prdoc`) and the documented purpose of `ReentrancyProtection::AllowNext`. A Devin session with repo/test access would be needed to run `Stipends.sol`'s `testTransferReentrancy` with a nested call sized to fit within the granted stipend weight to confirm real fund drainage end-to-end.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L192-203)
```rust
) -> ControlFlow<Halt> {
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

**File:** prdoc/stable2603/pr_11227.prdoc (L6-12)
```text
    \ handles the 2300 gas stipend\n\n| Solidity call | value | gas passed by compiler\
    \ | Stipend source |\n|---|---|---|---|\n| `target.transfer(amount)` | > 0 | `0`\
    \ | EVM adds 2300 automatically |\n| `target.send(amount)` | > 0 | `0` | EVM adds\
    \ 2300 automatically |\n| `target.transfer(0)` | 0 | `2300` | Compiler injects\
    \ explicitly |\n| `target.send(0)` | 0 | `2300` | Compiler injects explicitly\
    \ |\n| `target.call{value: v}(\"\")` | any | remaining gas | No stipend (forwards\
    \ all gas) |\n\nThe zero-value case is the one detected by our `gas_limit == CALL_STIPEND`\
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
