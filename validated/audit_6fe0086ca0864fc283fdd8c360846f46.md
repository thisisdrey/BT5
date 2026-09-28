### Title
An attacker can permanently block collateral removal by leaving a dust `DepositToken` balance - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.removeDepositToken()` refuses to remove a collateral token while its `DepositToken.totalSupply()` is non-zero. Any unprivileged user can create that supply through the public `DepositToken.deposit()` function and retain a dust amount, causing every subsequent removal attempt to revert. Because there is no administrative forcible burn or migration path in the shown code, the governor cannot remove or replace that collateral configuration while the attacker refuses to withdraw. [1](#0-0) [2](#0-1) 

### Finding Description
The governor-facing removal function enforces a strict zero-supply invariant before deleting the underlying-asset mapping and removing the token from the pool's collateral list. [1](#0-0) 

That invariant is externally influenceable because `deposit()` is public, requires only a non-zero amount and beneficiary, transfers the attacker's underlying to the Treasury, and mints deposit tokens to the chosen beneficiary. [2](#0-1) 

The mint path increases `totalSupply` and records the recipient's balance, so even a dust deposit creates the non-zero supply that blocks removal. [3](#0-2) 

Although withdrawals burn supply, withdrawal is voluntary and there is no administrative path in `removeDepositToken()` to force the attacker's dust position to close. [4](#0-3) [1](#0-0) 

### Impact Explanation
An attacker can permanently prevent removal of a listed collateral token for the cost of one dust deposit. [1](#0-0) [2](#0-1) 

This denies the protocol's collateral lifecycle operation and prevents replacing the deposit-token configuration through the intended remove-and-re-add path, since adding another deposit token for the same underlying is separately blocked by `UnderlyingAssetInUse`. [5](#0-4) 

The attack does not directly freeze existing user withdrawals because `_withdraw()` remains callable while the deposit token exists and the pool is not shut down. [6](#0-5) 

### Likelihood Explanation
The attack requires no privileged role, timing dependency, oracle manipulation, flash loan, or large capital commitment. [2](#0-1) 

The attacker only needs enough underlying to mint a positive amount of deposit tokens and can retain that position indefinitely. [3](#0-2) 

Disabling the deposit token prevents further minting but does not remove existing dust supply or enable `removeDepositToken()` to succeed. [7](#0-6) [8](#0-7) 

### Recommendation
Do not require `DepositToken.totalSupply()` to be zero merely to delist the collateral; instead, explicitly stop new deposits and provide a governed migration or settlement path for remaining holders. [1](#0-0) 

Alternatively, add a governed force-withdrawal or redemption mechanism that burns remaining deposit tokens and sends the corresponding underlying to their owners before completing removal. [6](#0-5) 

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {DepositToken} from "../contracts/DepositToken.sol";
import {IERC20} from "../contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";

contract RemoveDepositTokenDosTest is Test {
    error TotalSupplyIsNotZero();

    Pool pool;
    DepositToken depositToken;
    IERC20 underlying;
    address attacker = address(0xA77ACC);

    function testDustDepositBlocksRemoveDepositToken() external {
        uint256 dust = 1;

        deal(address(underlying), attacker, dust);

        vm.startPrank(attacker);
        underlying.approve(address(depositToken), dust);
        depositToken.deposit(dust, attacker);
        vm.stopPrank();

        assertGt(depositToken.totalSupply(), 0);

        vm.prank(pool.governor());
        vm.expectRevert(TotalSupplyIsNotZero.selector);
        pool.removeDepositToken(depositToken);

        // The attacker keeps the dust position and can repeat this indefinitely
        // for any later removal attempt unless all dust is withdrawn or burned.
    }
}
```

### Citations

**File:** contracts/Pool.sol (L698-708)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();

        depositTokenOf[_underlying] = IDepositToken(depositToken_);

```

**File:** contracts/Pool.sol (L737-743)
```text
    function removeDepositToken(IDepositToken depositToken_) external onlyGovernor {
        if (depositToken_.totalSupply() > 0) revert TotalSupplyIsNotZero();

        if (!depositTokens.remove(address(depositToken_))) revert DepositTokenDoesNotExist();
        delete depositTokenOf[depositToken_.underlying()];

        emit DepositTokenRemoved(depositToken_);
```

**File:** contracts/DepositToken.sol (L211-236)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
```

**File:** contracts/DepositToken.sol (L444-462)
```text
    function _burn(address _account, uint256 _amount) private updateRewardsBeforeMintOrBurn(_account) {
        if (_account == address(0)) revert BurnFromTheZeroAddress();

        uint256 _balanceBefore = balanceOf[_account];
        if (_balanceBefore < _amount) revert BurnAmountExceedsBalance();
        uint256 _balanceAfter;
        unchecked {
            _balanceAfter = _balanceBefore - _amount;
            totalSupply -= _amount;
        }

        balanceOf[_account] = _balanceAfter;

        emit Transfer(_account, address(0), _amount);

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L469-488)
```text
    function _mint(
        address account_,
        uint256 amount_
    ) private onlyIfDepositTokenIsActive updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert MintToTheZeroAddress();

        totalSupply += amount_;
        if (totalSupply > maxTotalSupply) revert SurpassMaxDepositSupply();

        uint256 _balanceBefore = balanceOf[account_];
        unchecked {
            balanceOf[account_] = _balanceBefore + amount_;
        }

        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
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

**File:** contracts/DepositToken.sol (L559-563)
```text
    function toggleIsActive() external override onlyGovernor {
        bool _newIsActive = !isActive;
        emit DepositTokenActiveUpdated(_newIsActive);
        isActive = _newIsActive;
    }
```
