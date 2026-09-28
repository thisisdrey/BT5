### Title
Attacker can permanently fill a victim's per-account token list via dust deposits/transfers, blocking all new deposits and borrows (UserReachedMaxTokens) - (File: contracts/Pool.sol)

### Summary
The TensorFlow advisory class is "a container is sized/validated against an assumed-valid input whose invariant the code never checks" — the first `dense_shape` element is trusted to be a positive batch count. In Metronome the analogous unchecked container invariant is the per-account token set: `Pool.debtTokensOfAccount`/`depositTokensOfAccount` (a `MappedEnumerableSet`) are capped at `MAX_TOKENS_PER_USER = 30`, and an unprivileged attacker can fill a victim's list to the cap by sending dust `DepositToken`s / forcing dust debt entries, after which the victim can never receive a new token type.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` [1](#0-0) [2](#0-1) . These functions are invoked unconditionally from `DepositToken._mint` and `DepositToken._transfer` whenever the recipient's balance goes from 0 to non-zero [3](#0-2) [4](#0-3) , and likewise from `DebtToken` issuance.

An attacker deploys/uses existing pool deposit tokens, deposits a tiny amount of each listed collateral, and `transfer`s 1 wei of each `DepositToken` to the victim (deposits via `deposit(amount_, onBehalfOf_ = victim)` also work since `onBehalfOf_` is attacker-chosen [5](#0-4) ). Each dust transfer permanently adds one entry to the victim's `depositTokensOfAccount`. With ~30 distinct registered deposit tokens (or by combining dust debt positions on multiple `DebtToken`s), the victim hits `MAX_TOKENS_PER_USER`.

The transfer itself succeeds — the victim cannot refuse receipt — but every subsequent action that would add a *new* token type to the victim's list reverts: `deposit`/`transfer` of any deposit token the victim doesn't yet hold reverts inside `_mint`/`_transfer` [6](#0-5) , and `DebtToken.issue`/mint for a new synthetic reverts via `addToDebtTokensOfAccount` [7](#0-6) . Even `Pool.liquidate` on the victim reverts whenever `_fee > 0` and the seized token is new to `feeCollector`... more precisely, liquidation works for existing entries but the victim cannot rotate into a new collateral to cure an unhealthy position.

The victim can only recover by spending a transaction per unwanted token to move the dust balance to zero so `removeFromDepositTokensOfAccount` fires [8](#0-7)  — which fails if the dust is locked as collateral (`_revertIfLocked` [9](#0-8) ), i.e., a victim with outstanding debt may be unable to shed enough entries to ever borrow or deposit again.

### Impact Explanation
Temporary to potentially indefinite freezing of the victim's ability to open new positions and receive tokens: new deposits to the victim revert, new borrows revert, and (if dust is locked by an open debt position) the victim cannot remove entries to unblock themselves. While debt is outstanding the lock is enforced by `unlockedBalanceOf` [10](#0-9) , so the griefed list entries cannot be cleared — the victim is permanently capped at their current token set until debt is repaid.

### Likelihood Explanation
Requires no privileged role: `DepositToken.deposit`, `transfer`, and `DebtToken` issuance are all public, and `onlyIfAdditionWillNotReachMaxTokens` is the only gate, with no opt-out for recipients. Cost is bounded by dust on up to 30 registered collaterals (governor must have registered ≥30 tokens for the pure-deposit variant; fewer needed when combining debt-token entries). Feasible on any deployed pool with enough listed tokens.

### Recommendation
Do not count forced-dust entries against the cap, or make the cap non-blocking: e.g., allow `addTo*TokensOfAccount` to succeed past the cap but only enforce `MAX_TOKENS_PER_USER` in user-initiated paths (`deposit` for `msg.sender`, `issue`/`mint`), or let recipients clear entries regardless of lock status when the balance is below a dust threshold. Alternatively, auto-remove on receipt if recipient already at cap rather than reverting the whole transfer.

### Proof of Concept
Foundry fork test sketch:

```solidity
function test_griefMaxTokens() public {
    // pool has 30 deposit tokens dt[0..29] registered
    for (uint i; i < 30; ++i) {
        underlying[i].approve(address(dt[i]), 1);
        dt[i].deposit(1, victim);        // onBehalfOf_ = victim
        // or: dt[i].transfer(victim, 1);
    }
    assertEq(pool.getDepositTokensOfAccount(victim).length, 30);

    // Any new token type to victim now reverts
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    dtNew.deposit(1, victim);

    // New borrow reverts via addToDebtTokensOfAccount
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    debtToken.issue(victim, 1e18);
}
```

Confirmed code paths: `onlyIfAdditionWillNotReachMaxTokens` [1](#0-0) , hooks in `_mint`/`_transfer` [11](#0-10) .

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/Pool.sol (L204-220)
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
    }
```

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L211-237)
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
    }
```

**File:** contracts/DepositToken.sol (L383-398)
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
    }
```

**File:** contracts/DepositToken.sol (L483-525)
```text
        emit Transfer(address(0), account_, amount_);

        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }

    /// @inheritdoc TokenHolder
    // solhint-disable-next-line no-empty-blocks
    function _requireCanSweep() internal view override onlyGovernor {}

    /**
     * @notice Move `amount` of tokens from `sender` to `recipient`
     */
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
