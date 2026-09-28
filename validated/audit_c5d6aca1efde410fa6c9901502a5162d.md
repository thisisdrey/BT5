### Title
Unsolicited deposit-token dust can exhaust a victim's token slots and block their chosen position - ([File: contracts/DepositToken.sol])

### Summary
An unprivileged attacker can call `DepositToken.deposit(amount_, victim)` or transfer a dust `DepositToken` balance to a victim, causing the victim's per-account deposit-token list to grow without the victim's consent. When the victim's combined debt-token and deposit-token lists reach `Pool.MAX_TOKENS_PER_USER`, `Pool.addToDepositTokensOfAccount()` and `Pool.addToDebtTokensOfAccount()` revert, temporarily preventing the victim from opening a different collateral or debt position until an entry is removed. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`DepositToken.deposit()` accepts an arbitrary `onBehalfOf_` recipient and only requires that address to be non-zero; the underlying is pulled solely from the caller, while newly minted deposit tokens are assigned to `onBehalfOf_`. [1](#0-0)  The mint path adds the token to the recipient's account list whenever their prior balance is zero, and the same occurs for an ordinary deposit-token transfer to a new holder. [4](#0-3) [5](#0-4) 

`Pool` enforces a shared limit of 30 entries across a user's debt-token and deposit-token sets. [6](#0-5) [2](#0-1)  The attacker can therefore occupy the remaining entries with dust balances in supported deposit tokens, causing `UserReachedMaxTokens` when the victim later tries to add a preferred deposit token or create a new debt-token entry. [3](#0-2) 

### Impact Explanation
The impact is temporary denial of position creation and forced acceptance of an unwanted collateral-token entry rather than theft. A victim whose list is full cannot deposit a new collateral type or create a new debt-token position until they remove an existing deposit-token balance. [2](#0-1) [7](#0-6)  This is especially relevant when the victim is one slot below the cap: an attacker can front-run the victim's intended deposit or borrowing transaction and force the last slot to be consumed by a less favorable token. [1](#0-0) 

The victim can generally recover by transferring or withdrawing the unsolicited deposit-token balance, after which `_burn()` or `_transfer()` removes zero-balance entries from the per-account list. [7](#0-6) [8](#0-7)  Accordingly, the practical impact is a griefing/temporary-liveness issue bounded by the cost of obtaining the dust and the victim's remediation transaction.

### Likelihood Explanation
The attack requires the victim to be near `MAX_TOKENS_PER_USER` and requires the attacker to obtain at least a minimal balance in the deposit tokens used to fill the remaining entries. [6](#0-5) [9](#0-8)  No privileged role is required because `deposit()`, `transfer()`, and `transferFrom()` are public entry points; the attacker only pays for the underlying dust and transaction execution. [1](#0-0) [10](#0-9) 

`nonReentrant` and pause checks do not prevent the attack because the attacker calls `deposit()` while the contract is unpaused and does not need nested calls. [11](#0-10)  The recipient is not required to authorize the incoming deposit or transfer, and there is no signature or opt-in check before `pool.addToDepositTokensOfAccount(account_)` is invoked. [12](#0-11) [13](#0-12) 

### Recommendation
Require recipient consent before a new token type is added to `depositTokensOfAccount`, such as only allowing `onBehalfOf_ == _msgSender()` for `deposit()` or requiring a recipient signature for third-party deposits. [1](#0-0)  Alternatively, separate the ERC20 balance from the tracked collateral set so an incoming dust transfer does not consume a position slot unless the recipient explicitly activates that collateral. [14](#0-13) 

### Proof of Concept
The following Hardhat-style sequence demonstrates the core state transition against deployed-style contracts:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IDepositTokenPoC {
    function deposit(uint256 amount_, address onBehalfOf_) external returns (uint256, uint256);
    function transfer(address to_, uint256 amount_) external returns (bool);
    function balanceOf(address account_) external view returns (uint256);
}

interface IERC20PoC {
    function approve(address spender_, uint256 amount_) external returns (bool);
}

interface IPoolPoC {
    function getDepositTokensOfAccount(address account_) external view returns (address[] memory);
}

function testThirdPartyDepositConsumesAccountSlot() public {
    address victim = makeAddr("victim");
    IDepositTokenPoC depositToken = IDepositTokenPoC(DEPOSIT_TOKEN);
    IERC20PoC underlying = IERC20PoC(UNDERLYING);
    IPoolPoC pool = IPoolPoC(POOL);

    uint256 victimTokensBefore = pool.getDepositTokensOfAccount(victim).length;

    underlying.approve(address(depositToken), 1);
    depositToken.deposit(1, victim);

    uint256 victimTokensAfter = pool.getDepositTokensOfAccount(victim).length;

    assertEq(depositToken.balanceOf(victim), 1);
    assertEq(victimTokensAfter, victimTokensBefore + 1);
}
```

The call path is `deposit(1, victim)` → `_mint(victim, deposited)` → `pool.addToDepositTokensOfAccount(victim)`; the final call reverts with `UserReachedMaxTokens` if the victim already has 30 combined debt and deposit entries. [1](#0-0) [12](#0-11) [2](#0-1) [14](#0-13)

### Citations

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

**File:** contracts/DepositToken.sol (L348-373)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
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

**File:** contracts/DepositToken.sol (L459-462)
```text
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

**File:** contracts/DepositToken.sol (L498-525)
```text
    function _transfer(
        address sender_,
        address recipient_,
        uint256 amount_
    ) private updateRewardsBeforeTransfer(sender_, recipient_) {
        if (sender_ == address(0)) revert TransferFromTheZeroAddress();
        if (recipient_ == address(0)) revert TransferToTheZeroAddress();

        uint256 _senderBalanceBefore = balanceOf[sender_];
        if (_senderBalanceBefore < amount_) revert TransferAmountExceedsBalance();
        uint256 _recipientBalanceBefore = balanceOf[recipient_];

        unchecked {
            balanceOf[sender_] = _senderBalanceBefore - amount_;
            balanceOf[recipient_] += amount_;
        }

        emit Transfer(sender_, recipient_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/Pool.sol (L76-80)
```text
    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;

```

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-219)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }

    /**
     * @notice Add a deposit token to the per-account list
     * @dev This function is called from `DepositToken` when user's balance changes from `0`
     * @dev The caller should ensure to not pass `address(0)` as `_account`
     * @param account_ The account address
     */
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
```
