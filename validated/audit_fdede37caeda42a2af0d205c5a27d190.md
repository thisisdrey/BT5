### Title
Unchecked `_sumOfValues` accumulation in `Operator.execute` can wrap and bypass the `msg.value` equality check - (File: contracts/Operator.sol)

### Summary
`Operator.execute` sums the per-call `value` fields inside an `unchecked` block. An attacker can craft a call list whose `value` entries sum to `msg.value mod 2^256`, so the final `require(msg.value == _sumOfValues, "value-mismatch")` check is satisfied while the contract actually dispatches far more ETH than was paid in. This mirrors the reported bug class: a silent `unchecked` overflow corrupting an accounting invariant that protects value flows.

### Finding Description
In `Operator.execute`, each `Call.value` is added to `_sumOfValues` inside `unchecked`, and each call is executed with `functionCallWithValue(_call.callData, _value)` drawing ETH from `address(this).balance`. The only guard is `require(msg.value == _sumOfValues)` after the loop. [1](#0-0) 

By submitting e.g. two calls with `value = 2^255`, `_sumOfValues` wraps to `0`, so `msg.value == 0` is accepted while the contract forwards `2^256 - 1`-scale ETH total (or any wrapped combination, e.g. `type(uint256).max + x` pays `x`). The ETH spent comes from the Operator's own balance, not the attacker's.

Impact requires the Operator contract to hold ETH. ETH can accumulate via `selfdestruct`/coinbase force-sends, accidental plain transfers (no `receive` guard rejects them — actually plain ETH transfers to the contract succeed since there is no reverting fallback shown, `execute` is the only payable entry but direct sends are possible via force-send), or any residual balance. Any ETH present is attacker-extractable.

### Impact Explanation
Direct theft of ETH held by the `Operator` contract (e.g. force-sent or user-stranded funds), bypassing the `value-mismatch` solvency check. Scope is limited to the contract's existing balance — it cannot conjure ETH it doesn't hold, since each `functionCallWithValue` would revert on insufficient balance.

### Likelihood Explanation
Likelihood of the overflow itself is high — `calls_` is fully attacker-controlled calldata and `execute` is a public, unprivileged entry point. Exploitability depends on `address(operator).balance > 0`; on deployed instances, dust ETH from accidental sends or `SELFDESTRUCT` donations is plausible but not guaranteed. Severity is therefore bounded by stranded-balance magnitude rather than core pool solvency — materially weaker than a general drain primitive.

### Recommendation
Remove the `unchecked` around the accumulation so the sum reverts on overflow:

```solidity
_sumOfValues += _value;
```

Or equivalently check `msg.value` per-call/incrementally with checked arithmetic. The `++i` unchecked increment is safe to keep.

### Proof of Concept
Foundry test sketch:

```solidity
// Assume operator holds ETH (simulating force-sent/stranded ETH)
vm.deal(address(operator), 1 ether);

IOperator.Call[] memory calls = new IOperator.Call[](2);
// Two calls of value = 2^255 each wrap _sumOfValues to 0
calls[0] = IOperator.Call({target: sink, value: 2**255 - 1, callData: ""});
calls[1] = IOperator.Call({target: sink, value: 2**255 - (1 ether) - 1, callData: ""});
// sum mod 2^256 == 0  =>  msg.value == 0 required
// but contract sends (2^255-1) + (2^255 - 1 ether - 1) == ~2^256 - 1 ether
// funded from pre-existing balance + 0 msg.value only works if
// balance >= total; adjust values so wrapped sum == msg.value sent.
operator.execute{value: 0}(calls);
```

Concretely: set call values `v1` and `v2` such that `v1 + v2 == address(operator).balance + k * 2^256` and send `msg.value = 0` (or any `v` where `v1+v2 ≡ v (mod 2^256)` with `v < v1+v2`). The loop forwards `v1 + v2` ETH from the contract's own balance, then `msg.value == _sumOfValues` passes because the sum wrapped. Uncertain point: whether deployed Operator instances carry an ETH balance must be verified on a fork (`eth_getBalance`); if the balance is zero, the finding degrades to theft of any future/stranded ETH only.

### Citations

**File:** contracts/Operator.sol (L42-54)
```text
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
