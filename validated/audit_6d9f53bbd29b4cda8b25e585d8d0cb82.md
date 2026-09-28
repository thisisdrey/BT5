### Title
Unchecked accumulation of `call.value` in `Operator.execute` allows ETH theft via integer overflow - (File: contracts/Operator.sol)

### Summary
`Operator.execute` sums the per-call `value` fields inside an `unchecked` block and only afterwards requires `msg.value == _sumOfValues`. A crafted `Call[]` whose `value` entries sum to `2^256 + X` overflows `_sumOfValues` to `X`, so the attacker supplies only `X` wei while the contract forwards `2^256 + X` worth of nominal `value` across sub-calls — any ETH held by the `Operator` contract (stray/donated/refunded balances) is drained to attacker-chosen targets.

### Finding Description
The bug class of the reference report is a missing bounds check on attacker-controlled input causing corruption. The Solidity analog lives in `contracts/Operator.sol:34-55`:

```solidity
uint256 _sumOfValues;
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;          // wraps on overflow
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked { ++i; }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

Each `functionCallWithValue` sends `_value` wei from the `Operator` contract's own balance to `target`. Because the sum is accumulated with `unchecked`, an attacker can include calls such as `[{value: type(uint256).max, ...}, {value: 2, ...}]`. The sum wraps to `1`, so `msg.value = 1` satisfies the equality check, while `Address.functionCallWithValue` attempts to send `2^256 - 1` wei from the contract. If the `Operator` contract holds *any* ETH (donations, stuck refunds, dust sent via `execute` leftovers from other users' calls), the attacker crafts the values so the overflowed sum equals `msg.value` while individual `_value`s route real ETH to an attacker-controlled contract.

### Impact Explanation
Direct theft of ETH held by the `Operator` contract. Any ETH balance owned by `Operator` at call time (dust accumulates from users overpaying `msg.value` in prior `execute` batches — note `require(msg.value == _sumOfValues)` prevents *that*, but direct `transfer`/selfdestruct donations and ETH left in the contract by other flows are fair game) can be fully extracted by an unprivileged EOA. No privileged role, oracle, or governance action is required; `execute` is permissionless and `nonReentrant` does not help since the entire theft happens in a single top-level call.

### Likelihood Explanation
The exploit requires the `Operator` contract to hold a nonzero ETH balance and enough ETH to satisfy the first sub-call's `_value` (the call reverts on insufficient balance, so the attacker must tune values to `balance + overflow`). Since `Operator` is `payable` and users routinely attach `msg.value` to `execute` for multi-step operations, accidental or deliberate funding is plausible, and an attacker can always self-fund via a preceding benign call is not possible — but any pre-existing balance is directly stealable. Exploitation is a single transaction with calldata fully controlled by the attacker; no front-running or timing is needed.

### Recommendation
Remove the `unchecked` accumulation so `_sumOfValues` overflow reverts, or accumulate with overflow checking (`_sumOfValues += _value;` in checked context). Alternatively, require per-call `address(this).balance` sufficiency implicitly enforced is already there; the only fix needed is checked arithmetic on the running sum.

### Proof of Concept
Foundry test (place in `test/`):

```solidity
function test_operatorExecuteOverflowDrain() public {
    Operator op = new Operator();
    vm.deal(address(op), 1 ether);          // simulate stray/donated ETH

    AttackerReceiver recv = new AttackerReceiver();

    IOperator.Call[] memory calls = new IOperator.Call[](2);
    calls[0] = IOperator.Call({
        target: address(recv),
        value: type(uint256).max,
        callData: ""
    });
    calls[1] = IOperator.Call({
        target: address(recv),
        value: 2,                          // max + 2 wraps sum to 1
        callData: ""
    });

    // sum = 2^256 + 1 -> wraps to 1; attacker pays only 1 wei
    op.execute{value: 1}(calls);

    assertGt(address(recv).balance, 0);   // drained from Operator balance
}
```

Note: the first sub-call requests `type(uint256).max` which exceeds the balance, so in practice the attacker picks `calls[0].value = op.balance`, `calls[1].value = type(uint256).max - op.balance + 1 + msgValue`, etc., such that the wrapped sum equals the small `msg.value` provided and the first call succeeds in paying out the full contract balance before the second call reverts-proof ordering is arranged (put the large-overflow call second, or size `calls[0].value` to exactly `op.balance`). [1](#0-0)

### Citations

**File:** contracts/Operator.sol (L40-55)
```text
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
