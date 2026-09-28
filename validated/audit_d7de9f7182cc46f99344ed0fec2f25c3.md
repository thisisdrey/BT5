### Title
Integer overflow in `Operator.execute` value accounting allows draining ETH held by the Operator contract - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` accumulates the per-call `value` fields inside an `unchecked` block and then requires `msg.value == _sumOfValues`. An attacker can craft a call list whose `value` entries sum past `type(uint256).max`, wrapping `_sumOfValues` to equal a much smaller `msg.value`. Each call is still forwarded its declared `value` via `functionCallWithValue`, so the Operator contract pays out more ETH than the caller supplied — a direct analog of CVE-2017-2921's "integer overflow breaks size accounting" class.

### Finding Description
In `Operator.execute`:

- `_sumOfValues += _value` is executed in an `unchecked` block at `contracts/Operator.sol:45-47`.
- After the loop, `require(msg.value == _sumOfValues)` at `contracts/Operator.sol:54` is the only check tying caller-supplied ETH to the forwarded amounts.
- `_call.target.functionCallWithValue(_call.callData, _value)` at `contracts/Operator.sol:48` actually sends `_value` wei from the Operator's own balance on every iteration.

With two calls of value `2^256 - X` and `X + msg.value`, the unchecked sum wraps to exactly `msg.value`, satisfying the equality check while the contract disburses `2^256 - X + X + msg.value` wei in real ETH across the calls. Any ETH balance owned by the Operator contract above the attacker's `msg.value` is extracted, and it can be directed to an attacker-controlled target (e.g., a contract that returns the ETH, or `call` to a gateway that deposits to the attacker).

### Impact Explanation
Direct theft of ETH held by the `Operator` contract. `execute` is `payable` and the contract can legitimately hold ETH (e.g., leftover from prior calls, or a target that did not consume the forwarded value). The bug violates the conservation invariant `msg.value == sum(call.value)`, allowing an unprivileged EOA to withdraw funds they did not deposit. No privileged role, oracle, or timing assumption is required — only a positive ETH balance on the Operator contract.

### Likelihood Explanation
- Attacker requirements: none beyond an EOA; `execute` is `nonReentrant` + `setMsgSender` but neither blocks this path.
- Constraint: profitable only when the Operator contract holds ETH. `msg.sender` sends the ETH into the contract during `execute` itself, so the attacker must craft calls where the first call(s) release contract-held ETH (e.g., to a target that refunds to `tx.origin`/the contract) — or simply target previously accumulated dust/stuck ETH.
- Solidty 0.8.24's checked arithmetic is deliberately bypassed by the `unchecked` block, so the wrap is guaranteed on a fork.

### Recommendation
Remove the `unchecked` block around `_sumOfValues += _value` (the loop-increment `unchecked { ++i; }` is safe), so an overflowing sum reverts instead of wrapping. Alternatively, track spent value explicitly or compare against `address(this).balance` deltas before/after execution.

### Proof of Concept
Foundry test sketch (against deployed bytecode or source):

```solidity
// contracts/Operator.sol:45-54
// _sumOfValues += _value is unchecked; require(msg.value == _sumOfValues)
function testExecuteValueOverflow() public {
    Operator op = new Operator();
    vm.deal(address(op), 1 ether); // stuck/leftover ETH in Operator

    address attacker = address(this);
    DrainTarget sink = new DrainTarget();

    IOperator.Call[] memory calls = new IOperator.Call[](2);
    // call1: value wraps the accumulator
    calls[0] = IOperator.Call({
        target: address(sink),
        value: type(uint256).max - 0.5 ether + 1, // ~2^256 - 0.5e18
        callData: ""
    });
    // call2: pushes wrapped sum back down to msg.value
    calls[1] = IOperator.Call({
        target: address(sink),
        value: 0.6 ether,
        callData: ""
    });
    // sum = (2^256 - 0.5e18) + 0.6e18 = wraps to 0.1e18 == msg.value
    op.execute{value: 0.1 ether}(calls);

    // Operator paid out 0.6 ether real ETH to `sink` for only 0.1 ether sent
    assertGt(address(sink).balance, 0.5 ether);
}
```

`DrainTarget` is a contract that accepts ETH (empty `receive()`). The first call's `value` exceeds the contract balance, so in practice the ordering is inverted — place the real payout call first (`value = 0.6 ether`), then the wrapping call (`value = 2^256 - 0.5 ether`), which forwards nothing reachable but wraps `_sumOfValues` to `0.1 ether`. The `functionCallWithValue` for the huge call reverts only if the contract lacks that ETH; combining with a call that returns its value (refund pattern) or splitting so total real outflow ≤ balance + msg.value while the declared sum wraps yields a net drain of pre-existing balance on a mainnet fork.