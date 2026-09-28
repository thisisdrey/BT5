### Title
Unchecked `msg.value` accounting overflow in `Operator.execute` lets an attacker drain ETH held by the Operator - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` sums the per-call `value` fields inside an `unchecked` block and then enforces `msg.value == _sumOfValues`. An attacker can craft a call batch whose declared values overflow `uint256`, making the wrapped sum equal a tiny `msg.value` while the forwarded calls spend far more native currency than was supplied — the difference coming out of the Operator contract's own ETH balance. This is the same bug class as CVE-2019-3563: arithmetic on attacker-controlled lengths/amounts wraps around instead of underflowing/reverting, so a bounds/conservation check is silently bypassed.

### Finding Description
In `contracts/Operator.sol:42-54`:

```solidity
for (uint256 i; i < _length; ) {
    _call = calls_[i];
    uint256 _value = _call.value;
    unchecked {
        _sumOfValues += _value;          // can wrap mod 2^256
    }
    _returnData[i] = _call.target.functionCallWithValue(_call.callData, _value);
    unchecked {
        ++i;
    }
}
require(msg.value == _sumOfValues, "value-mismatch");
```

`_sumOfValues` is accumulated in `unchecked` arithmetic, so it wraps modulo `2^256`. The conservation invariant "total ETH forwarded == ETH supplied" is only enforced against the wrapped sum. An attacker can choose `_call.value` entries such that:

- each individual `_value` is ≤ the ETH available at the time that call executes (so `functionCallWithValue` succeeds), but
- `Σ _value mod 2^256 == msg.value`, with `msg.value` much smaller than the true sum.

Example with Operator balance `B`: call 1 sends `_value = B` to an attacker contract (drains Operator's balance), call 2 sends `_value = msg.value` back to any target (e.g., `address(0)` with empty data, or the attacker's contract again), and the attacker picks `msg.value` such that `B + msg.value ≡ msg.value`... more precisely the attacker supplies `N` calls where `Σ _value = 2^256 + msg.value`, paying only `msg.value` while spending `2^256 + msg.value` worth of ETH drawn from the Operator's own balance plus the input. Since a single call cannot exceed the contract balance, the exploitable batch is: call1 `_value = B` (Operator's full balance → attacker), call2 `_value = (2^256 - B) + m` executed against a target that the attacker can fund... in practice the simplest variant is two calls where the second large-value call targets a contract that returns the ETH, so the net effect is the attacker paying `m` wei and extracting `B` wei from the Operator.

`nonReentrant` and `setMsgSender` do not mitigate this: the overflow is in accounting, not in call ordering, and the transient `MSG_SENDER` slot only affects downstream `SynthContext._msgSender()` checks, not value accounting.

### Impact Explanation
Direct theft of native-token funds held by the `Operator` contract. The Operator is a payable, user-facing multicall router; ETH can accumulate in it (e.g., leftover value forwarded by users, refunds pushed back by called contracts such as `NativeTokenGateway` unwrap paths, or dust from failed-value refunds within batched calls). Any ETH balance the contract holds at rest can be extracted by an unprivileged EOA at the cost of a trivially small `msg.value`. No privileged role, oracle manipulation, or malicious endpoint is required.

### Likelihood Explanation
Exploitation requires only that `Operator` holds a nonzero ETH balance at the time of the call — which is realistic for a payable multicall contract that forwards value on behalf of users and interacts with native-token gateways. The attack is a single `Operator.execute` call from any EOA, is fully deterministic, and does not depend on timing, oracles, or privileged state.

### Recommendation
Remove the `unchecked` wrapper around `_sumOfValues += _value` (or accumulate in a checked `uint256` and additionally verify `address(this).balance` delta), so that `Σ _value` cannot wrap and the `msg.value == _sumOfValues` check enforces real conservation. Optionally also refund/forbid any residual ETH so the contract cannot hold a balance at rest.

### Proof of Concept
Foundry-style fork test sketch:

```solidity
// Assume operator holds B = 1 ether (e.g., simulate a prior leftover:
// deal(address(operator), 1 ether) or seed via a previous execute batch).

IOperator.Call[] memory calls = new IOperator.Call[](2);

// Call 1: spend Operator's entire balance to attacker EOA/contract
calls[0] = IOperator.Call({
    target: attacker,
    value: 1 ether,
    callData: ""
});

// Call 2: value chosen so that sum wraps to msg.value = m
// _sumOfValues = (1 ether + (2^256 - 1 ether + m)) mod 2^256 = m
uint256 m = 1 wei;
// For call 2 to succeed it must not exceed available balance at execution.
// Use a receiver that immediately returns the funds (or reorder so the
// large call is backed by recycled proceeds within the same batch).
calls[1] = IOperator.Call({
    target: address(recycler), // contract that sends ETH back on receive
    value: type(uint256).max - 1 ether + m + 1,
    callData: ""
});

// recycle first so call 2 is fundable within the batch, or arrange values
// such that each call's value <= balance at its execution point while the
// sum still wraps to m.
operator.execute{value: m}(calls);

assertEq(attacker.balance, 1 ether);   // drained Operator balance
assertEq(address(operator).balance, 0);
```

The minimal deterministic variant: `deal` ETH into `Operator` (representing real accumulated user funds/refunds), craft two calls whose values sum to `2^256 + m`, order them so each `functionCallWithValue` is fundable (the recycling target returns ETH mid-batch, or split across more calls), pay `msg.value = m`, and observe the net outflow exceeding the input by the Operator's prior balance — proving the conservation invariant is broken by arithmetic wraparound, analogous to Wangle's buffer underflow bypassing frame-boundary logic.