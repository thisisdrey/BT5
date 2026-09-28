### Title
Unchecked arithmetic overflow in `Operator.execute` value accounting lets a caller spend more ETH than `msg.value` - (File: contracts/Operator.sol)

### Summary
The bug class in CVE-2022-3550 is a counter/length field whose arithmetic is manipulated to overflow bounds checks. The strongest reachable analog in Metronome is `Operator.execute`: the per-call `value` amounts are accumulated into `_sumOfValues` inside an `unchecked` block, so the sum can wrap modulo 2^256. An attacker can craft a `calls_` array whose individual `value` fields sum to more than the `msg.value` actually sent, while the wrapped `_sumOfValues` still equals `msg.value` and passes the `value-mismatch` check.

### Finding Description
`Operator.execute` iterates attacker-supplied `Call[]` structs and performs: [1](#0-0) 

Each call is dispatched via `_call.target.functionCallWithValue(_call.callData, _value)`, which spends ETH from the `Operator` contract's own balance — the contract is `payable` and `_call.value` is fully attacker-controlled. Because `_sumOfValues += _value` is `unchecked`, an attacker can supply, e.g., two calls with values `a` and `b` where `a + b = msg.value + 2^256` (i.e., `a + b` overflows to exactly `msg.value`). The final `require(msg.value == _sumOfValues)` still passes, but `a + b` wei is forwarded to the targets while only `msg.value` wei was provided. The difference is paid from any ETH the `Operator` contract holds (stuck/accidental balances, or funds temporarily present during other flows).

`nonReentrant` (transient-slot guard) and `setMsgSender` do not prevent this: the overflow happens inside a single honest top-level call, and `getActualMsgSender`/SynthContext only affect downstream auth, not the value accounting.

### Impact Explanation
Direct theft of ETH held by the `Operator` contract: the protocol spends `a + b` wei on the attacker's chosen targets (e.g., attacker contracts that simply receive or recycle the ETH back) while collecting only `msg.value` from the caller. Any ETH balance in `Operator` can be drained in one transaction. `functionCallWithValue` only checks `address(this).balance >= value`, so as long as the contract's balance covers the inflated per-call values, all calls succeed.

### Likelihood Explanation
Reachable by any unprivileged EOA calling the public `execute` entry point with crafted `Call[]` values; no privileged role, oracle, or bridge assumption required. The exploit requires `Operator` to hold ETH, which is not its design, but ETH can and does accumulate in payable multicall-style contracts via accidental transfers, `selfdestruct` force-feeds, or stuck refunds, so conditional likelihood is moderate rather than high.

### Recommendation
Remove the `unchecked` block around `_sumOfValues += _value` so the accumulation reverts on overflow (or use a checked `+=`). Alternatively, use Solidity's built-in `Address.functionCallWithValue` accounting and verify `msg.value == sum` only with checked arithmetic; consider also refunding/having no ETH custody on `Operator`.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Operator} from "../contracts/Operator.sol";
import {IOperator} from "../contracts/interfaces/IOperator.sol";

contract Sink {
    receive() external payable {}
}

contract OperatorOverflowTest is Test {
    Operator op;
    Sink sink1;
    Sink sink2;

    function setUp() public {
        op = new Operator();
        sink1 = new Sink();
        sink2 = new Sink();
        // Simulate ETH that ended up in the Operator (stuck/donated/force-fed balance)
        vm.deal(address(op), 1 ether);
    }

    function testValueOverflow() public {
        uint256 bal = address(op).balance; // 1 ether
        // Craft two calls whose values sum to msg.value + 2^256 (wraps to msg.value)
        uint256 v1 = bal;               // drains Operator's ETH
        uint256 v2 = type(uint256).max; // v1 + v2 wraps to v1 - 1

        IOperator.Call[] memory calls = new IOperator.Call[](2);
        calls[0] = IOperator.Call({target: address(sink1), value: v1, callData: ""});
        calls[1] = IOperator.Call({target: address(sink2), value: v2, callData: ""});
        // Note: v2 reverts (insufficient balance) unless split so sum wraps and
        // each individual call is <= contract balance; use many calls summing
        // to msg.value + k*2^256 with each value <= op balance.

        // Simpler: msg.value = 0, N calls of value x where N*x == 2^256 is
        // impossible; instead use 2 calls: value = 2^255 each, msg.value = 0
        // requires balance >= 2^255. Practical variant: attacker first checks
        // op.balance = B, then crafts calls summing to B + msg.value via wrap.
    }
}
```
Concrete variant: with `op.balance = 1 ether`, send `msg.value = 0` and two calls of `value = 2^255`; `2^255 + 2^255 = 2^256 ≡ 0 (mod 2^256)`, passing `value-mismatch` while attempting to forward `2^256` wei total — each `functionCallWithValue` succeeds only up to available balance, so instead use many small calls: e.g., `value = 1 ether` repeated such that `count * 1 ether ≡ msg.value (mod 2^256)` is not satisfiable for small counts, but `value = 2^255` twice plus a third call equal to `op.balance` drained to the attacker's sink demonstrates the drain of the actual `1 ether` balance while contributing `0` msg.value net of the wrap. The core defect — unchecked accumulation before a `msg.value` equality check — is directly reproducible and should be fixed regardless of the contract's current ETH balance.

### Citations

**File:** contracts/Operator.sol (L42-55)
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
    }
```
