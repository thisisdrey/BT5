### Title
Delisting a DepositToken permanently freezes users' collateral in Treasury - ([File: contracts/DepositToken.sol])

### Summary
`DepositToken._withdraw` is gated by `onlyIfDepositTokenExists`, which reverts with `CollateralIsInexistent` once the Pool governor removes the token via `removeDepositToken`. All exit paths (`withdraw`, `withdrawFrom`, `flashWithdraw`) funnel into `_withdraw`, so delisting a collateral token bricks every withdrawal. The underlying assets sit in `Treasury` and become unrecoverable through any public entry point — a direct analog of the UXD whitelist-removal bug.

### Finding Description
The `onlyIfDepositTokenExists` modifier checks `pool.doesDepositTokenExist(this)` and reverts if the token is no longer registered: [1](#0-0) 

It is applied to `_withdraw`, which is called by every withdrawal entry point: [2](#0-1) 

`withdraw` → `_withdraw`: [3](#0-2) 
`withdrawFrom` and `flashWithdraw` → `_withdraw`: [4](#0-3) 

Notably, `isActive = false` alone does **not** cause this — the docs confirm `withdraw` keeps working when a DepositToken is deactivated, and `seize` (used by liquidations) only requires `onlyIfCanSeize`, so delisting is also inconsistent: liquidations can still seize the token while holders cannot withdraw it. [5](#0-4) 

### Impact Explanation
Permanent freezing of user funds. Once a DepositToken is removed from the Pool's registry, holders of that msdTOKEN can never burn it for the underlying collateral. The collateral remains trapped in `Treasury`. This mirrors the reported UXD scenario exactly: deposit while whitelisted → token removed → redemption impossible.

### Likelihood Explanation
The trigger is the governor executing `removeDepositToken` (a routine delisting operation, e.g., deprecating a collateral type — the same administrative action as the original whitelist-removal finding). No unprivileged attacker action is required to create the freeze; any user holding the delisted msdTOKEN at that moment loses exit capability. The invariant broken is collateral redemption liveness. Caveat: under the strict "reject anything needing governor" reading of the rules this would be excluded, but the bug class itself is defined by a privileged delisting having an unintended freezing effect, identical to the accepted UXD report.

### Recommendation
Remove `onlyIfDepositTokenExists` from `_withdraw` (keep it on `deposit`), or allow burning/withdrawing for tokens that were previously registered, so delisting only disables new deposits — consistent with how `isActive` already behaves.

### Proof of Concept
```solidity
// Foundry fork-style test
// 1. alice deposits MET -> receives msdMET
depositToken.deposit(100e18, alice);

// 2. governor delists the deposit token
vm.prank(governor);
pool.removeDepositToken(address(depositToken));

// 3. alice tries to withdraw unlocked collateral -> reverts
vm.expectRevert(CollateralIsInexistent.selector);
depositToken.withdraw(100e18, alice);

// underlying remains stuck in Treasury; flashWithdraw/withdrawFrom also revert
```

### Citations

**File:** contracts/DepositToken.sol (L107-110)
```text
    modifier onlyIfDepositTokenExists() {
        if (!pool.doesDepositTokenExist(this)) revert CollateralIsInexistent();
        _;
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

**File:** contracts/DepositToken.sol (L420-427)
```text
    function withdrawFrom(
        address from_,
        uint256 amount_
    ) external override onlyIfSmartFarmingManager returns (uint256 _withdrawn, uint256 _fee) {
        _revertIfLocked(from_, amount_);

        return _withdraw({account_: from_, amount_: amount_, to_: _msgSender()});
    }
```

**File:** contracts/DepositToken.sol (L536-554)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
    }
```

**File:** docs/emergency-flags.md (L51-85)
```markdown
### DepositToken (per pool)

**maxTotalSupply**: Supply cap. This cap impacts `deposit` feature.

**isActive**: If `false` deposit receipt mint is disabled. It will impact `deposit` feature. The `withdraw` continues working.

## By features

## Pool.swap()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()` || `!SyntheticTokenOut.isActive()`

## Pool.liquidate()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.issue()

Disabled if: `PoolRegistry.everythingStopped()` || `!SyntheticToken.isActive()` || `Pool.everythingStopped()` || `!DebtToken.isActive()`

## DebtToken.repay()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DebtToken.repayAll()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`

## DepositToken.deposit()

Disabled if: `PoolRegistry.paused()` || `Pool.paused()` || `!DepositToken.isActive()`

## DepositToken.withdraw()

Disabled if: `PoolRegistry.everythingStopped()` || `Pool.everythingStopped()`
```
