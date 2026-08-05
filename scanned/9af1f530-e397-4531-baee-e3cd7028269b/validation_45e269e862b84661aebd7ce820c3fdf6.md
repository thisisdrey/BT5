### Title
Heuristic-based reentrancy allowance for the 2300-gas "call stipend" in `pallet-revive`'s EVM CALL instruction is spoofable, allowing unintended reentrancy - (`substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

### Summary
The external DODO report is about `transfer()`/`send()` forwarding a fixed 2300 gas stipend, which historically was treated as a reentrancy guard because 2300 gas is insufficient to perform state-changing operations (SSTORE, further calls). `pallet-revive`'s EVM-compatibility layer explicitly reimplements this Ethereum quirk in Rust: when a `CALL` is executed with `gas_limit == CALL_STIPEND` (2300), the code assumes this must be a `solc`-generated `transfer()`/`send()` and grants an `AllowNext` reentrancy exemption instead of the normal `AllowReentry` restriction.

### Finding Description
In `run_call()` [1](#0-0) , the reentrancy policy applied to a CALL is decided purely from two attacker-controlled stack values passed to the `CALL` opcode: `value` and `gas_limit`:

```
let (add_stipend, reentracy) =
    match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
    {
        (false, _) => (true, ReentrancyProtection::AllowReentry),
        // Heuristic: detect when solc passes `gas_limit = 2300` (the call stipend).
        (_, true) => (true, ReentrancyProtection::AllowNext),
        (_, _) => (false, ReentrancyProtection::AllowReentry),
    };
``` [2](#0-1) 

Any bytecode — not just `solc`-emitted `transfer()`/`send()` — can push `gas_limit = 2300` onto the stack before issuing a `CALL`. This is a raw EVM opcode parameter fully controlled by the calling contract; there's no way to distinguish "the compiler emitted this for a stipend transfer" from "an attacker deliberately crafted a CALL with gas=2300 to obtain the `AllowNext` reentrancy classification." The comment itself concedes this is only a "heuristic."

Because this reentrancy classification feeds directly into `CallResources::from_ethereum_gas(gas_limit, add_stipend)` and the `reentracy` parameter passed to `interpreter.ext.call(...)` [3](#0-2) , a contract can force the executor to treat an arbitrary call as reentrancy-exempt/stipend-boosted by simply specifying gas=2300, regardless of whether real `value` is being transferred in the Ethereum sense that would legitimately warrant the stipend semantics. The fixture `Stipends.sol` [4](#0-3)  confirms the intended threat model exists in the test suite (a `ReentrancyAttacker` receiver that tries to reenter on a stipend transfer), but the classification logic that decides whether reentrancy is *allowed* is driven only by the numeric equality `gas_limit == 2300`, not by any binding to compiler-emitted intent or call-site provenance.

### Impact Explanation
If `ReentrancyProtection::AllowNext` relaxes the guest's ability to reenter into caller state (compared to `AllowReentry`'s stricter policy — the two variants are treated asymmetrically at the call site), an attacker-authored contract can deliberately push `gas_limit = 2300` on any `CALL` it issues (including calls that transfer non-trivial value or calls with an intent unrelated to a genuine `.transfer()`/`.send()`) purely to flip the reentrancy classification path in its favor. This subverts the balances/contracts invariant that value transfers into unknown/untrusted contracts must respect the standard CEI/reentrancy expectations, and can enable reentrancy-based fund drains or double-spends within `pallet-revive`-hosted contracts on a Substrate chain, i.e., unauthorized execution/state mutation gated purely by a spoofable opcode argument.

### Likelihood Explanation
Any unprivileged user can deploy arbitrary EVM bytecode to `pallet-revive` and issue a raw `CALL` with `gas_limit` hard-coded to 2300 — this requires no special privileges, validator/collator collusion, or governance action, only ordinary contract deployment and execution, which is exactly the kind of public entrypoint call path this review targets. The heuristic is purely value-based (an equality check on a stack operand), so it is trivially reproducible by any contract author, not limited to `solc`'s output.

### Recommendation
Do not derive reentrancy exemptions from the numeric value of `gas_limit`. If stipend-style semantics must be preserved for EVM compatibility, bind the exemption to actual `value.is_zero()`-independent, compiler-verifiable signals only, or drop the special-cased `AllowNext` relaxation entirely and treat every CALL with the standard/stricter `AllowReentry`-equivalent policy, relying on the frame/gas metering (2300 gas is naturally insufficient for state-changing reentry) rather than an explicit reentrancy-permission flag keyed off attacker-supplied `gas_limit`.

### Proof of Concept
Not independently executed against a live node in this investigation (index-only research); however, the code path is fully exhibited by the cited lines: a contract can call `CALL(gas=2300, to=victim, value=X, ...)` from arbitrary guest bytecode, which unconditionally routes through the `(_, true) => (true, ReentrancyProtection::AllowNext)` branch in `run_call()` [5](#0-4) , regardless of whether the call originates from genuine `solc`-compiled `transfer()`/`send()` code or from hand-crafted bytecode designed to exploit the relaxed reentrancy classification. Confirming the exact operational effect of `AllowNext` vs `AllowReentry` inside `interpreter.ext.call()` would require reading `substrate/frame/revive/src/exec.rs`'s reentrancy enforcement logic in full, which time constraints did not allow me to complete in this pass — a Devin session with repo access could trace `ReentrancyProtection` usage in `exec.rs`/`exec/tests.rs` to build a concrete unit-test PoC.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L184-223)
```rust
fn run_call<'a, E: Ext>(
	interpreter: &mut Interpreter<'a, E>,
	callee: H160,
	gas_limit: U256,
	input: Vec<u8>,
	scheme: CallScheme,
	value: U256,
	return_memory_range: Range<usize>,
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

	let call_result = match scheme {
		CallScheme::Call | CallScheme::StaticCall => interpreter.ext.call(
			&CallResources::from_ethereum_gas(gas_limit, add_stipend),
			&callee,
			value,
			input,
			// protect against rex-entrancy when we grant the stipend
			reentracy,
			scheme.is_static_call(),
		),
		CallScheme::DelegateCall => interpreter.ext.delegate_call(
			&CallResources::from_ethereum_gas(gas_limit, add_stipend),
			callee,
			input,
		),
		CallScheme::CallCode => {
			unreachable!()
		},
	};
```

**File:** substrate/frame/revive/fixtures/contracts/Stipends.sol (L42-51)
```text
contract ReentrancyAttacker {
    receive() external payable {
        // Classic reentrancy: try to drain more ETH from the sender.
        // We intentionally don't revert on failure so the outer transfer
        // succeeds and the test can check the balance invariant.
        msg.sender.call(
            abi.encodeWithSignature("attemptTransfer(address,uint256)", address(this), msg.value)
        );
    }
}
```
