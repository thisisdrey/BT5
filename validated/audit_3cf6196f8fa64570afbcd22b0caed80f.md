### Title
`VesperGateway.withdraw` burns the user's full deposit position when `IVPool.withdraw` silently returns zero or partial underlying - (File: contracts/VesperGateway.sol)

### Summary
`VesperGateway.withdraw` first withdraws and burns the full msdTOKEN position, converts the received vToken shares through an external Vesper pool, and then accepts whatever underlying balance delta is returned, including zero. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
The public `withdraw` function validates only that `pool_` is registered, transfers `amount_` msdTOKEN from the caller to the gateway, and invokes the registered `DepositToken.withdraw`. [1](#0-0) [2](#0-1) 

`DepositToken._withdraw` burns the full withdrawn msdTOKEN amount and pulls the corresponding vToken collateral from `Treasury` before the gateway calls the external vault. [4](#0-3) [5](#0-4) 

`IVPool.withdraw(uint256)` returns no amount and its interface does not require a full withdrawal or a revert when the requested shares cannot be redeemed. [6](#0-5) 

Metronome's own AMO code explicitly documents that a Vesper pool may withdraw less than requested and therefore measures the returned balance delta rather than assuming success. [7](#0-6) 

The gateway uses that balance delta as the amount sent to the user but has no `minUnderlyingOut`, nonzero-output check, or other postcondition. [8](#0-7) [9](#0-8) 

If `vToken_.withdraw(_vTokenAmount)` redeems only part of the shares or successfully returns without paying any underlying, the transaction still succeeds and the user receives only the observed delta while their entire msdTOKEN position has already been consumed. [10](#0-9) [9](#0-8) 

### Impact Explanation
A user who withdraws through the gateway during a Vesper liquidity shortfall permanently loses the difference between the expected collateral and the underlying actually paid. [10](#0-9) [9](#0-8) 

For a silent zero withdrawal, the user loses the entire `amount_` because the msdTOKEN was burned and the vToken shares were either consumed by the pool or left under gateway control while no underlying is returned. [11](#0-10) [3](#0-2) 

This breaks the withdrawal identity invariant: surrendering `amount_` deposit shares should either return the full corresponding collateral or revert, not commit the position and return an arbitrary partial amount without user-specified slippage consent. [12](#0-11) 

### Likelihood Explanation
The vulnerable path is directly reachable by an unprivileged caller through `VesperGateway.withdraw(pool_, vToken_, amount_)` whenever `vToken_` is registered collateral in a registered pool. [13](#0-12) 

The `nonReentrant` guard does not prevent the issue, and the only pool-level operational check inside `DepositToken._withdraw` is `whenNotShutdown`. [1](#0-0) [14](#0-13) 

No privileged actor is required: a user can encounter the condition naturally after the external Vesper pool's immediately available liquidity falls below the redemption amount, and Metronome already treats partial Vesper withdrawals as a real behavior. [7](#0-6) 

### Recommendation
Add a user-controlled `minUnderlyingOut_` parameter to `VesperGateway.withdraw` and revert when the observed underlying delta is below it. [1](#0-0) [3](#0-2) 

For compatibility without changing the public signature, at minimum require `_underlyingAmount > 0`; however, this prevents only total failure and still allows an unconsented partial withdrawal, so a minimum-output parameter is the safer fix. [3](#0-2) 

For example:

```solidity
// contracts/VesperGateway.sol
function withdraw(
    IPool pool_,
    IVPool vToken_,
    uint256 amount_,
    uint256 minUnderlyingOut_
) external override nonReentrant {
    ...
    uint256 _balanceBefore = _underlying.balanceOf(address(this));
    vToken_.withdraw(_vTokenAmount);
    uint256 _underlyingAmount = _underlying.balanceOf(address(this)) - _balanceBefore;

    if (_underlyingAmount < minUnderlyingOut_) revert WithdrawalSlippageTooHigh();

    _underlying.safeTransfer(_msgSender, _underlyingAmount);
}
```

Because a revert rolls back the earlier msdTOKEN transfer, burn, Treasury pull, and external vToken withdrawal, checking the delta after `vToken_.withdraw` is sufficient to preserve the all-or-minimum withdrawal invariant. [15](#0-14) 

### Proof of Concept
The following Foundry fork test uses the deployed Metronome contracts and mocks only the external `IVPool.withdraw` edge case—successfully returning without paying underlying—to demonstrate that the production gateway commits the user's position anyway. [3](#0-2) 

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";
import {IVPool} from "../contracts/interfaces/external/IVPool.sol";
import {IPool} from "../contracts/interfaces/IPool.sol";
import {IDepositToken} from "../contracts/interfaces/IDepositToken.sol";
import {VesperGateway} from "../contracts/VesperGateway.sol";

contract VesperGatewaySilentWithdrawTest is Test {
    function testSilentVesperWithdrawLosesUserPosition() external {
        // Configure these for a deployed chain where vToken is registered collateral.
        IPool pool = IPool(vm.envAddress("POOL"));
        VesperGateway gateway = VesperGateway(vm.envAddress("VESPER_GATEWAY"));
        IVPool vToken = IVPool(vm.envAddress("VTOKEN"));
        IERC20 underlying = IERC20(vToken.token());
        IDepositToken msd = pool.depositTokenOf(vToken);

        require(address(msd) != address(0), "vToken is not registered");

        address user = makeAddr("user");
        uint256 collateralAmount = 100e18;

        // Setup only: give the user vToken collateral and create an msdTOKEN position.
        deal(address(vToken), user, collateralAmount);

        vm.startPrank(user);
        vToken.approve(address(msd), collateralAmount);
        msd.deposit(collateralAmount, user);
        vm.stopPrank();

        uint256 position = msd.balanceOf(user);
        assertGt(position, 0);

        (uint256 expectedShares, ) = msd.quoteWithdrawOut(position);
        uint256 underlyingBefore = underlying.balanceOf(user);

        vm.prank(user);
        msd.approve(address(gateway), position);

        // A compliant non-reverting external withdrawal edge: it consumes/retains the
        // redemption request but transfers zero underlying.
        vm.mockCall(
            address(vToken),
            abi.encodeWithSelector(IVPool.withdraw.selector, expectedShares),
            ""
        );

        vm.prank(user);
        gateway.withdraw(pool, vToken, position);

        assertEq(underlying.balanceOf(user), underlyingBefore, "user received zero underlying");
        assertEq(msd.balanceOf(user), 0, "entire msdTOKEN position was consumed");
        assertEq(
            vToken.balanceOf(address(gateway)),
            expectedShares,
            "withdrawn collateral is stranded in the gateway"
        );
    }
}
```

The assertions show the exact fund-loss state: the transaction succeeds, the user's msdTOKEN balance is consumed, no underlying is returned, and the withdrawn collateral remains outside the user's control. [16](#0-15)

### Citations

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

**File:** contracts/DepositToken.sol (L536-540)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
```

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
```

**File:** contracts/interfaces/external/IVPool.sol (L10-12)
```text
    function deposit(uint256 _amount) external;

    function withdraw(uint256 _shares) external;
```

**File:** contracts/AMO.sol (L142-146)
```text
        // Vesper pool may withdraw less, of course burn less shares too, than requested.
        // Hence the difference of Synth balance after withdraw and before withdraw is actual Synth withdrawn.
        uint256 _before = syntheticToken_.balanceOf(address(this));
        vPool_.withdraw(shares_);
        return syntheticToken_.balanceOf(address(this)) - _before;
```
