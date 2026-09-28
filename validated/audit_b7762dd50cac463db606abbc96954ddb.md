### Title
Integer overflow in `Operator.execute` value accounting allows draining ETH held by the Operator - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` accumulates the per-call `value` fields inside an `unchecked` block and then enforces `msg.value == _sumOfValues`. Because the addition wraps modulo 2^256, an attacker can craft a batch whose individual `value` entries sum to more ETH than is actually sent, and spend ETH already sitting in the `Operator` contract. This is the same bug class as CVE-2021-29477: an integer overflow in a size/value computation that lets the caller make the system disburse more than it accounted for.

### Finding Description
In `contracts/Operator.sol:40-54`, the loop sums each call's `value` in an `unchecked` block:

```solidity
uint256 _sumOfValues;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;   // wraps on overflow
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked { ++i; }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

The check `msg.value == _sumOfValues` is intended to guarantee the caller funded exactly the total ETH forwarded to targets. With `unchecked` addition, two calls of `value = 2^255` wrap `_sumOfValues` to `0`, so the attacker satisfies the equality with `msg.value = 0` while the contract attempts to forward `2^255 + 2^255` worth of ETH. `functionCallWithValue` reverts if the contract balance is insufficient, so the practical impact is bounded by the ETH the `Operator` contract actually holds — but any ETH held there (leftover from calls that pushed refunds back to `Operator`, e.g., a target like a WETH-unwrapping gateway or `NativeTokenGateway` flow returning ETH mid-batch, donations, or `selfdestruct` dust) can be taken with zero payment. A simpler variant: `calls = [{value: B}, {value: 2^256 - B}]` with `msg.value = 0` forwards exactly the contract's balance `B` to an attacker-controlled target while the wrapped sum is 0.

Invariants and guards checked:
- `nonReentrant` and `setMsgSender` do not constrain arithmetic; they only guard reentrancy and set the transient sender slot (`contracts/Operator.sol:20-36`).
- No length cap or per-call value check exists; `calls_` and `value` are fully attacker-controlled.
- The wrapped-sum equality is the only value-conservation check, and it is bypassed by overflow.

### Impact Explanation
Direct theft of ETH held by the `Operator` contract. ETH can legitimately reside in `Operator` transiently (e.g., a target that refunds ETH to `msg.sender` = `Operator` during a batch, or ETH pushed via `selfdestruct`/donation). Such funds belong to users mid-flow or are otherwise unclaimable except by the configured logic; the overflow lets any unprivileged EOA extract them by wrapping `_sumOfValues` to `msg.value`. Impact is capped by the Operator's ETH balance at the time of the call.

### Likelihood Explanation
Requires `address(operator).balance > 0` at execution time, which is situational: the contract is designed to be a pass-through and normally holds no ETH. However, any call target that sends ETH back to `msg.sender` inside a batch (refund-style receivers such as the `NativeTokenGateway` unwrap path or user-supplied targets) leaves ETH in `Operator` for the remainder of the transaction, and an attacker can chain a draining batch afterward or in a later transaction. No privileged role, oracle manipulation, or trusted-remote misbehavior is needed — just a public `execute` call with crafted `value` fields.

### Recommendation
Remove the `unchecked` block around `_sumOfValues += _value` so overflow reverts naturally, or accumulate with a running check (`require(_value <= msg.value - _spent)`) and compare `msg.value` against the actual forwarded total.

### Proof of Concept
Foundry test sketch (assumes `Operator` is deployed and holds 1 ETH, e.g., pushed via `selfdestruct` or a refunding call):

```solidity
function testOverflowDrain() public {
    // fund operator with 1 ETH (simulating stuck/refunded ETH)
    vm.deal(address(operator), 1 ether);

    IOperator.Call[] memory calls = new IOperator.Call[](2);
    calls[0] = IOperator.Call({
        target: payable(attacker),
        callData: "",
        value: 1 ether
    });
    calls[1] = IOperator.Call({
        target: payable(attacker),
        callData: "",
        value: type(uint256).max // 1 ether + max wraps _sumOfValues to 0
    });

    vm.prank(attacker);
    operator.execute{value: 0}(calls); // passes msg.value == _sumOfValues (== 0 wrapped)

    assertEq(attacker.balance, 1 ether);
    assertEq(address(operator).balance, 0);
}
```

The first call forwards the contract's real 1 ETH; the second call's `value = type(uint256).max` would revert on send unless combined appropriately — more precisely, use `calls[0].value = operatorBalance` and `calls[1].value = 2**256 - operatorBalance` so the wrapped sum is exactly 0 while the first call drains the full balance. Reproducible on a Hardhat/Foundry fork by first getting ETH into `Operator` (e.g., a batch calling a trivial contract that does `selfdestruct(payable(operator))` or an ETH-returning target), then running the draining batch.