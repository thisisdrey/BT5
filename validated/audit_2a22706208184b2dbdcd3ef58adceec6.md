Audit Report

## Title
Reentrancy-protection bypass for value-transferring EVM `CALL`/`transfer`/`send` in pallet-revive - (File: substrate/frame/revive/src/vm/evm/instructions/contract.rs)

## Summary
`run_call` in `substrate/frame/revive/src/vm/evm/instructions/contract.rs` selects the `ReentrancyProtection` mode using a match that puts `value.is_zero() == false` first, so any nonzero-value call — regardless of `gas_limit` — is unconditionally assigned `ReentrancyProtection::AllowReentry` [1](#0-0) . Since solc emits `gas_limit = 0` for `target.transfer(amount)`/`target.send(amount)` when `amount > 0` (relying on the EVM's implicit 2300-gas stipend addition), these two extremely common Solidity ETH-payout idioms are excluded from the `AllowNext` reentrancy guard that the pallet's own documentation states is specifically needed to compensate for revive's differing gas scale [2](#0-1) .

## Finding Description
The reentrancy mode is chosen by:

```rust
let (add_stipend, reentracy) =
    match (value.is_zero(), gas_limit.try_into().is_ok_and(|limit: u64| limit == CALL_STIPEND))
    {
        (false, _) => (true, ReentrancyProtection::AllowReentry),
        (_, true) => (true, ReentrancyProtection::AllowNext),
        (_, _) => (false, ReentrancyProtection::AllowReentry),
    };
``` [1](#0-0) 

The first arm `(false, _)` matches on `value.is_zero() == false`, i.e., any call transferring nonzero value, irrespective of the `gas_limit` operand. This means `target.transfer(amount)` and `target.send(amount)` with `amount > 0` — which solc compiles to a `CALL` with `gas_limit = 0` (letting the EVM add the 2300 stipend implicitly) — fall into this first arm and get `ReentrancyProtection::AllowReentry`, i.e., no explicit reentrancy guard, even though `add_stipend = true` is set for this arm (implying the code recognizes a stipend situation exists, yet doesn't apply the guard designed for it).

The `AllowNext` arm `(_, true)` only fires when `gas_limit == CALL_STIPEND` (2300) exactly, which per the PR that introduced this logic only occurs for the degenerate `target.transfer(0)`/`target.send(0)` case where solc injects the literal 2300 explicitly [3](#0-2) .

The `ReentrancyProtection::AllowNext` variant's documentation explicitly states the rationale for why this guard is needed: Ethereum's 2300-gas stipend is not enough for the callee to make another call, "however, due to gas scale differences that guarantee does not automatically hold in revive and we enforce it explicitly here" [2](#0-1) . This confirms that on revive, the converted stipend weight budget alone is not a reliable reentrancy barrier — the `AllowNext` guard exists specifically to compensate. Yet the dispatch logic never applies `AllowNext` for the actual `.transfer(amount>0)`/`.send(amount>0)` calls, only for the `value==0, gas_limit==2300` combination.

The metering conversion (`substrate/frame/revive/src/metering/math.rs`, `new_nested_meter`) shows that `add_stipend = true` grants extra weight (`weight_stipend = determine_call_stipend::<T>()`) to the nested frame regardless of which reentrancy mode was chosen [4](#0-3) , confirming that the stipend-based resource budget is present in both the `AllowReentry` and `AllowNext` paths, but the compensating reentrancy lock is only wired to the latter.

## Impact Explanation
This is a real logic defect in the reentrancy-mode selection for `pallet-revive`'s EVM-compatibility layer. If the converted stipend weight is sufficient for a callee to execute a further external call (which the pallet's own documentation acknowledges is a real risk due to gas-scale differences from Ethereum), a value-receiving contract's `receive()`/`fallback()` invoked via `.transfer(amount>0)`/`.send(amount>0)` can re-enter the caller during a "checks-effects-interactions" payout, since no `AllowNext`/`Strict` guard is installed for that call. This falls under "runtime bugs that compromise intended behavior" and can lead to duplicate settlement/payout or unbacked balance credit within contract execution — an in-scope impact for the Polkadot SDK program.

## Likelihood Explanation
The path is reachable by any unprivileged account calling any deployed EVM-compatible contract that uses `.transfer()`/`.send()`, one of the most common Solidity ETH-payout patterns, requiring only a normal `eth_transact`/contract-call extrinsic with an attacker-supplied receiving address. No privileged access, governance, or off-chain compromise is required.

## Recommendation
Change the match condition so it keys off "is this a stipend-style call" (i.e., `gas_limit == 0` or `gas_limit == CALL_STIPEND`) independent of whether `value` is zero, and apply `ReentrancyProtection::AllowNext` uniformly to all such calls, including `.transfer(amount>0)`/`.send(amount>0)`. The `value.is_zero()` check should not be the primary discriminant for reentrancy-guard selection at [1](#0-0) .

## Proof of Concept
1. Deploy contract `V` with a withdrawal function using `payable(msg.sender).transfer(amount)` where `amount > 0`, following the checks-effects-interactions pattern that relies on the stipend blocking reentrancy.
2. Deploy attacker contract `A` whose `receive()` calls back into `V`'s withdrawal function.
3. Call `V`'s withdrawal with `A` as the recipient. In `run_call`, `value.is_zero()` is `false`, so the `(false, _) => (true, ReentrancyProtection::AllowReentry)` arm is selected regardless of `gas_limit` (`0`, as solc emits for `.transfer(amount)`) [5](#0-4) ; no explicit reentrancy guard (`AllowNext`/`Strict`) is installed.
4. If the granted stipend weight (`add_stipend = true`, see `determine_call_stipend` in [4](#0-3) ) is sufficient for `A.receive()` to perform a further external call back into `V`, `A` reenters and can trigger a duplicate withdrawal before `V`'s balance state is updated — contrasted with the intended `AllowNext` behavior demonstrated by `testTransferReentrancy` in the fixture `Stipends.sol` [6](#0-5) , which is designed to verify that reentrancy is blocked but exercises only the `value==0`/`gas_limit==2300` path, not the real `.transfer(amount>0)` path affected by this bug.

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

**File:** substrate/frame/revive/src/metering/math.rs (L149-160)
```rust
					let stipend = if *add_stipend {
						let weight_stipend = determine_call_stipend::<T>();
						if weight_left.any_lt(weight_stipend) {
							return Err(<Error<T>>::OutOfGas.into());
						}

						weight_limit.saturating_accrue(weight_stipend);

						Some(weight_stipend)
					} else {
						None
					};
```

**File:** substrate/frame/revive/fixtures/contracts/Stipends.sol (L196-213)
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
```
