### Title
Operator.execute allows anyone to drain ERC20 tokens held by the Operator contract via arbitrary calls executed with `msg.sender == Operator` - (File: contracts/Operator.sol)

### Summary
`Operator.execute` performs fully attacker-controlled external calls (`target`, `callData`, `value`) with `msg.sender` set to the `Operator` contract itself, while the real EOA is only recorded in a transient slot consumed by `SynthContext._msgSender()`. Any contract that uses raw `msg.sender` — most notably every ERC20 `transfer`/`approve` — therefore treats the call as originating from `Operator`. An unprivileged attacker can call `execute` with `target = <any ERC20>` and `callData = transfer(attacker, balance)` to steal the entire token balance held by `Operator`.

### Finding Description
In `contracts/Operator.sol` lines 34–55, `execute` iterates over user-supplied `Call[]` structs and invokes `_call.target.functionCallWithValue(_call.callData, _value)` with no allowlist or validation of the target or calldata. The only sender bookkeeping is the transient `MSG_SENDER_STORAGE` slot (lines 20–24), which is only meaningful to contracts using `SynthContext._msgSender()` (`contracts/utils/SynthContext.sol` lines 14–24). All third-party/plain ERC20 logic — and in-scope contracts that read raw `msg.sender` like `RecurringAirdrop.claim` (`contracts/utils/RecurringAirdrop.sol` lines 52–65) — see `msg.sender == Operator`.

`Operator` accumulates tokens in practice: users batching a `RecurringAirdrop`/`MetAirdrop` claim through `Operator.execute` have rewards transferred to `Operator` (leaf/claimed bookkeeping is keyed by `msg.sender` = Operator), and any direct/erroneous transfers or dust land there. Since `Operator` has no rescue/sweep function, whoever calls `execute` with a `token.transfer(self, balanceOf(Operator))` payload owns those funds. The `nonReentrant` guard and `msg.value == _sumOfValues` check do not restrict this; the call requires zero ETH and zero privileges.

### Impact Explanation
Direct theft of user funds: any ERC20 balance held by `Operator` — including airdrop rewards claimed through it (`RecurringAirdrop._transferReward` sends `token` to `msg.sender` = Operator) and tokens users transfer to it accidentally — can be fully drained by any EOA in a single transaction. On deployed chains (mainnet `0xc06D...`, base, optimism, swell, hemi, plasma), `MetAirdrop` distributes MET/esMET via `RecurringAirdrop`, which is reachable and claimable through `Operator`, making trapped reward balances a realistic occurrence.

### Likelihood Explanation
The attack requires only that `Operator` hold a nonzero token balance — an expected state since the multicall is the documented way to batch protocol actions and `RecurringAirdrop.claim` unconditionally pays `msg.sender`. The attack itself is a permissionless single call with no capital, no privileged role, no oracle dependence, and no way to be blocked (no pause, no allowlist, no `onlyGovernor`).

### Recommendation
Reject calls in `Operator.execute` whose `target` is a token/known asset contract, or at minimum block `target == address(this)` patterns plus selector-level checks; alternatively add an explicit governor-only sweep function and revert `execute` calls to arbitrary ERC20 `transfer`/`approve`. Simplest robust fix: maintain an allowlist of callable targets (pool tokens, gateways, airdrop contracts) rather than allowing arbitrary targets, and never let `execute` produce calls where `msg.sender == Operator` against generic ERC20s.

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {IOperator} from "../contracts/interfaces/IOperator.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract OperatorDrainTest is Test {
    // Deployed on Base: Operator 0x64b5bb3b7eF0267019fee5b826c60Cb9B7609373
    IOperator operator = IOperator(0x64b5bb3b7eF0267019fee5b826c60Cb9B7609373);
    IERC20 token = IERC20(<any ERC20, e.g. MET or a token stuck in Operator>);
    address attacker = makeAddr("attacker");

    function testDrainTokensHeldByOperator() public {
        vm.createSelectFork(vm.envString("BASE_RPC_URL"));

        uint256 bal = token.balanceOf(address(operator));
        assertGt(bal, 0); // e.g. airdrop rewards paid to Operator or user dust

        IOperator.Call[] memory calls = new IOperator.Call[](1);
        calls[0] = IOperator.Call({
            target: address(token),
            value: 0,
            callData: abi.encodeCall(IERC20.transfer, (attacker, bal))
        });

        vm.prank(attacker);
        operator.execute(calls); // msg.sender inside token.transfer == Operator

        assertEq(token.balanceOf(address(operator)), 0);
        assertEq(token.balanceOf(attacker), bal);
    }
}
```