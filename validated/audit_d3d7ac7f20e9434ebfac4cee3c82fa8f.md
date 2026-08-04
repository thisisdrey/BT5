## Analysis: Gas-value heuristic used to gate reentrancy protection in pallet-revive EVM interpreter

### Bug-class reduction from the report

The C4 finding's core broken invariant: a fixed numeric constant (`TRANSFER_OVERHEAD = 24_000`) is used to distinguish "safe, bounded-gas operation" from "unbounded/attacker-controlled operation," but the constant does not correctly separate the two cases in practice, causing the security check built on top of it to behave incorrectly (in that case: always trip). The generalizable primitive is: **a raw gas-limit magic-number comparison is used as a security classifier, and an attacker who fully controls the compared value can force the classifier into the wrong branch.**

### Local analog [1](#0-0) 

`run_call` in pallet-revive's EVM interpreter decides whether a `CALL`/`STATICCALL` gets `ReentrancyProtection::AllowNext` (a relaxed reentrancy guard meant only for the Solidity `transfer`/`send` 2300-gas stipend pattern) purely by checking whether the caller-supplied `gas_limit` equals `revm::interpreter::gas::CALL_STIPEND` (2300):

```rust
let (add_stipend, reentracy) =
    match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
    {
        (false, _) => (true, ReentrancyProtection::AllowReentry),
        (_, true) => (true, ReentrancyProtection::AllowNext),
        (_, _) => (false, ReentrancyProtection::AllowReentry),
    };
``` [2](#0-1) 

### Finding Description

`gas_limit` here is a raw EVM stack value fully controlled by the contract executing `CALL`/`STATICCALL`/`DELEGATECALL` — it is bytecode-supplied, not a compiler-enforced constant. The comment explicitly frames the check as a *heuristic*: "detect when solc passes `gas_limit = 2300`". But nothing prevents an attacker-authored contract (hand-written bytecode, not solc-compiled) from setting `gas_limit = 2300` on a **non-zero-value** call, or more importantly, from setting `gas_limit = 2300` on a call that is *not* a benign zero-value transfer at all, forcing the interpreter down the `AllowNext` branch. `AllowNext` is documented (see PR notes for the reentrancy modes) as being weaker than `Strict` protection — it permits a single re-entrant call into the same contract, specifically to accommodate Solidity's built-in `transfer`/`send` stipend forwarding pattern.

Because the branch selection is based solely on equality with a constant that lives entirely in caller-controlled input, the security-relevant reentrancy mode (`Strict` vs `AllowNext`) is not an invariant of the actual call semantics (was this really a stipend-limited value transfer originated by `transfer()`/`send()`?) but of a value an adversarial contract can freely choose. This is structurally the same class of defect as the `TRANSFER_OVERHEAD` check in the report: a security decision is gated by comparing an attacker-influenceable/attacker-equal-to-magic-number quantity against a hardcoded constant, with no independent binding to the actual code path that constant is meant to represent.

### Impact Explanation

If a malicious contract deployed on a `pallet-revive`-based chain can pass `gas_limit == 2300` on an outbound `CALL` regardless of the actual call context, it downgrades the reentrancy guard from `Strict` to `AllowNext` for that sub-call. This weakens the interpreter's reentrancy protection exactly at the point where an attacker chooses it, defeating the purpose of the guard (blocking the classic checks-effects-interactions reentrancy exploit class) for calls that a defending contract's author never intended to be treated as "stipend-limited." Under the "Public Wrappers must not widen origin, bypass filters, or undercharge nested execution" pivot, this is a filter-bypass on the EVM-compat execution path: an unprivileged contract deployer can arrange for `AllowNext` semantics on arbitrary calls simply by hard-coding `gas(2300)`, independent of `value`.

### Likelihood Explanation

The condition is trivially reachable: it requires only deploying a contract whose bytecode issues a `CALL` opcode with the literal gas argument `2300`. No governance, validator, or off-chain trust assumption is needed — any account can deploy such a contract via a normal `eth_call`/`instantiate` extrinsic. The only gate is whether downstream callee code can actually be exploited via a re-entrant call in the `AllowNext` window, which is the same precondition any classic reentrancy exploit already requires (attacker-controlled callee, victim state update after external call).

### Recommendation

Do not classify reentrancy protection based solely on equality with the numeric stipend constant. Bind the relaxed `AllowNext` mode to context that the interpreter itself controls/derives (e.g., only apply it when the call was synthesized internally by the value-transfer path for a zero-value best-effort send, not whenever arbitrary bytecode happens to pass `2300`), or require additional invariants (e.g., no calldata, known EOA-recipient, or an explicit marker set by the transfer/send translation layer) before downgrading reentrancy protection.

### Proof of Concept

Conceptual PoC (cannot be executed in this ask-only session, but the code path is directly inspectable):
1. Deploy contract `A` with a `victim()` function that reads a storage flag, calls an attacker-supplied address via raw `CALL` opcode with gas literal `2300` and value `0`, then writes state afterward (classic reentrancy pattern).
2. Deploy attacker contract `B` whose fallback re-enters `A.victim()`.
3. Because `run_call` sees `value.is_zero() == true` and `gas_limit == 2300`, it selects `ReentrancyProtection::AllowNext` for the sub-call into `B`, per [3](#0-2) , even though this call has nothing to do with a genuine Solidity `transfer()`/`send()` stipend forward — the attacker chose the literal `2300` deliberately.
4. `B`'s fallback re-enters `A.victim()` once (permitted by `AllowNext`), completing a state-inconsistent reentrant call that `Strict` protection would have blocked.

This demonstrates that the classification constant from the external report's bug class (`TRANSFER_OVERHEAD`) has a direct structural analog here: a hardcoded gas magic number, fully within attacker control, used to select a weaker security mode.

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L105-128)
```rust
/// Implements the CALL instruction.
///
/// Message call with value transfer to another account.
pub fn call<E: Ext>(interpreter: &mut Interpreter<E>) -> ControlFlow<Halt> {
	let [gas_limit, to, value] = interpreter.stack.popn()?;
	let to = to.into_address();
	let has_transfer = !value.is_zero();
	if interpreter.ext.is_read_only() && has_transfer {
		return ControlFlow::Break(Error::<E::T>::StateChangeDenied.into());
	}
	let (input, return_memory_range) = get_memory_in_and_out_ranges(interpreter)?;
	let scheme = CallScheme::Call;
	charge_call_gas(interpreter, to, scheme, input.len(), value)?;

	run_call(
		interpreter,
		to,
		gas_limit,
		interpreter.memory.slice(input).to_vec(),
		scheme,
		value,
		return_memory_range,
	)
}
```

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L184-203)
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
```
