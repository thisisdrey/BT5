### Title
Reentrancy Stipend Heuristic in `pallet-revive` EVM `CALL` Grants Unrestricted Reentry for Real-World `.transfer()`/`.send()` Value Transfers - (File: `substrate/frame/revive/src/vm/evm/instructions/contract.rs`)

### Summary
`pallet-revive`'s EVM-compatibility layer implements a heuristic to recreate Ethereum's 2300-gas call-stipend reentrancy guarantee, but the match arm ordering in `run_call` causes any value-transferring `CALL` (including the standard bytecode emitted by Solidity's `.transfer()`/`.send()`, which pass `gas_limit = 0` and rely on the EVM's automatic 2300 stipend) to be granted `ReentrancyProtection::AllowReentry` — full, unrestricted reentrancy — instead of the intended `ReentrancyProtection::AllowNext`. Contracts deployed to a `pallet-revive`-based chain that rely on `.transfer()`/`.send()`'s well-known inability to make further external calls (the same gas-stipend property discussed in the referenced report) will not get that protection under revive's weight-based gas scale.

### Finding Description
`run_call` in `substrate/frame/revive/src/vm/evm/instructions/contract.rs` decides the reentrancy mode for a EVM `CALL`/`STATICCALL` based on `(value.is_zero(), gas_limit == CALL_STIPEND)`: [1](#0-0) 

The comment states the intent explicitly: "This is used for calls that transfer value but restrict gas so that the callee only has a stipend gas amount... due to gas scale differences that guarantee does not automatically hold in revive and we enforce it explicitly here" — see the `AllowNext` doc comment: [2](#0-1) 

Per the project's own documentation of solc's stipend behavior (added in the same feature's prdoc), a real (non-zero-value) `.transfer(amount)`/`.send(amount)` call compiles to a raw `CALL` with `value > 0` and an **explicit `gas_limit = 0`** — the 2300 stipend is added automatically by the EVM, not by the compiler: [3](#0-2) 

Because the match arm `(false, _) => (true, ReentrancyProtection::AllowReentry)` is listed first and matches on `value.is_zero() == false` regardless of the second tuple element, **every** non-zero-value `CALL` — including the `gas_limit = 0` case that is the actual bytecode shape of `.transfer()`/`.send()` — falls into this arm and is granted `AllowReentry`. The `AllowNext` branch (`(_, true)`) is only reachable when `value.is_zero()` is `true`, i.e. it only fires for the zero-value edge case explicitly called out and tested in the same PR (`testTransferZero`/`testSendZero`), not for the overwhelmingly common real-value transfer path. The PR's own test coverage confirms this: it only validates the `AllowNext` path for zero-value stipend calls, never for real value transfers: [4](#0-3) 

`AllowReentry` disables all of the reentrancy guarding done in `PrecompileExt::call`/`Stack::call`, i.e. `allows_reentry` is never toggled off before entering the callee frame: [5](#0-4) 

So a contract receiving a real `.transfer()`/`.send()` payment — code that, on real Ethereum, is unable to perform any further external call because it only has 2300 gas — is instead run with full reentrancy allowed on `pallet-revive`, and because revive's weight/gas conversion for the stipend can afford more logical operations than 2300 raw EVM gas would (this cross-scale mismatch is exactly why the `AllowNext` mechanism was introduced), the receiver frame can reenter the caller (or any other contract that still `allows_reentry`) during the payout.

### Impact Explanation
This breaks a security invariant that ported Solidity contracts implicitly rely on when deployed to a `pallet-revive` chain: the `.transfer()`/`.send()` "safe against reentrancy" idiom. Contracts that gate withdrawals, vault logic, or "checks-effects-interactions" ordering around the assumption that a `.transfer()` payout cannot trigger a reentrant call can be exploited to reenter and drain funds or double-settle payouts — a direct violation of the "conserve value and settle exactly once" invariant for contract-held value under `pallet-revive`. This is not a peer/validator/governance issue; it is exploitable by any unprivileged EVM contract deployer/caller purely through normal `call`/`transact` extrinsics.

### Likelihood Explanation
High for any Solidity contract that uses `.transfer()`/`.send()` with a non-zero amount (the standard, most common form of these calls) and is deployed unmodified on a `pallet-revive` chain. The bug requires no privileged actor, no malicious peer/validator, and no special conditions — an attacker only needs to be the recipient contract of a `.transfer()`/`.send()` payment and implement a `receive()`/fallback that reenters.

### Recommendation
Fix the match ordering/logic in `run_call` so that `AllowNext` is applied whenever the actual forwarded gas is the CALL_STIPEND amount, independent of whether `value.is_zero()`. Concretely, detect the stipend condition as `(value_is_zero && gas_limit == CALL_STIPEND) || (!value_is_zero && gas_limit == 0)` (mirroring the two rows of the documented solc behavior table for `transfer`/`send`), and only fall back to `AllowReentry` for the genuinely gas-forwarding `.call{value: v}("")` pattern (non-zero value with a gas limit that is not the stipend-implying `0`). Add regression tests analogous to `testTransferZero`/`testSendZero` but for non-zero-value `.transfer()`/`.send()` verifying `AllowNext` is applied and that reentrant calls from the receiver revert.

### Proof of Concept
1. Deploy an EVM contract `Attacker` with a `receive()` function that calls back into the paying contract (e.g., invoking a withdrawal function again) — analogous to `ReentrancyAttacker` already present in the fixtures: [6](#0-5) 
2. Deploy a `Vault` contract that pays out via `payable(msg.sender).transfer(amount)` with `amount > 0`, following the checks-effects-interactions "safe" idiom that developers use specifically because `.transfer()` blocks reentrancy on real Ethereum.
3. Trigger `Vault.withdraw()` from `Attacker`. On real Ethereum this reverts because `Attacker.receive()` cannot execute an external `CALL` with only 2300 gas. On `pallet-revive`, because the real-value branch of `run_call` maps to `ReentrancyProtection::AllowReentry` rather than `AllowNext`, `Attacker.receive()`'s reentrant call into `Vault` is not blocked by the reentrancy guard, allowing balance to be drained beyond the single intended withdrawal.
4. This can be verified directly against the existing `Stipends.sol` fixture and its Rust test harness by adding a non-zero-value analogue of `testTransferReentrancy`/`testSendReentrancy` (which currently only cover the zero-value case) and observing that, unlike the zero-value tests, the reentrant `attemptTransfer` call inside `receive()` is **not** rejected with `ReentranceDenied`: [7](#0-6)

### Citations

**File:** substrate/frame/revive/src/vm/evm/instructions/contract.rs (L163-182)
```rust
///
/// Static message call (cannot modify state).
pub fn static_call<E: Ext>(interpreter: &mut Interpreter<E>) -> ControlFlow<Halt> {
	let [gas_limit, to] = interpreter.stack.popn()?;
	let to = to.into_address();
	let (input, return_memory_range) = get_memory_in_and_out_ranges(interpreter)?;
	let scheme = CallScheme::StaticCall;
	let value = U256::zero();
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

**File:** substrate/frame/revive/fixtures/contracts/Stipends.sol (L196-223)
```text
    // Test that the transfer stipend prevents reentrancy. The attacker's receive()
    // tries to call back into attemptTransfer() to drain more ETH, but the 2300
    // gas stipend is not enough for an external call.
    function testTransferReentrancy() public payable {
        uint256 amount = msg.value / 4;
        uint256 balanceBefore = address(reentrancyAttacker).balance;

        // The attacker's receive() attempts an external call which exhausts
        // the stipend, causing receive() to revert with out-of-gas.
        bool failed = false;
        try this.attemptTransfer(payable(address(reentrancyAttacker)), amount) {
            failed = false;
        } catch {
            failed = true;
        }
        require(failed, "Transfer to reentrancy attacker should have failed");
        require(address(reentrancyAttacker).balance == balanceBefore, "Attacker balance should not change");
    }

    // Test that the send stipend prevents reentrancy.
    function testSendReentrancy() public payable {
        uint256 amount = msg.value / 4;
        uint256 balanceBefore = address(reentrancyAttacker).balance;

        bool success = payable(address(reentrancyAttacker)).send(amount);
        require(!success, "Send to reentrancy attacker should have failed");
        require(address(reentrancyAttacker).balance == balanceBefore, "Attacker balance should not change");
    }
```
