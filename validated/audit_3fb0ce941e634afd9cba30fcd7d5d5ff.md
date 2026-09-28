### Title
Unprivileged dust-transfer griefing fills a victim's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER`, reverting all new-collateral deposits and new-synthetic mints - (File: `contracts/DepositToken.sol`)

### Summary
`Pool` tracks each account's held deposit/debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . `DepositToken._transfer` pushes the token into the recipient's list whenever the recipient's prior balance was zero, without any opt-in [2](#0-1) . An attacker who holds dust of each listed `DepositToken` can `transfer(victim, 1 wei)` for every supported collateral, filling the victim's list. After that, `deposit(..., onBehalfOf_=victim)` and any `DebtToken` mint of a token not already in the victim's list revert in `onlyIfAdditionWillNotReachMaxTokens` [3](#0-2) .

### Finding Description
- `DepositToken.transfer` only checks the sender's unlocked balance via `_revertIfLocked`; the recipient is arbitrary [4](#0-3) .
- `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` on first receipt; the pool enforces `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30 → revert UserReachedMaxTokens` [5](#0-4) .
- The same cap check runs inside `_mint` (deposits) and `DebtToken` issuance, so once the list is full the victim cannot deposit a collateral type they don't already hold and cannot open debt in a new synthetic asset. The identical mechanism hits the pool's `feeCollector`: `_mint(feeCollector, _fee)` in `deposit` and `_transfer(account, feeCollector, _fee)` in `_withdraw` add the token to the collector's list; if the collector's slots are filled for a token whose fee balance is zero, every deposit and withdraw on that token reverts [6](#0-5) [7](#0-6) .

### Impact Explanation
Temporary freezing of funds / protocol liveness: targeted victims are blocked from adding new collateral types and from issuing new synthetic debt (revert on `UserReachedMaxTokens`), which prevents topping up a position with a different collateral when approaching liquidation. If the `feeCollector` list is filled, deposits and withdrawals on any token not already in its list revert protocol-wide until the collector sheds balances. The victim can recover by transferring/burning dust to free slots, but the attacker can re-dust, making this a repeatable griefing that breaks the deposit/mint liveness invariant.

### Likelihood Explanation
The attack is fully unprivileged: `transfer` is a public entry point, no admin role is needed, and `SynthContext`/pause/reentrancy modifiers do not restrict recipients. Cost is bounded by acquiring dust amounts of each listed deposit token (depositing minimum amounts of each underlying). Feasibility depends on the pool listing enough distinct deposit tokens to reach 30 combined slots for the target; on pools with fewer than 30 collaterals the pure-dust variant alone cannot hit the cap, limiting it to victims whose own debt tokens consume most slots.

### Recommendation
Whitelist only count tokens above a minimum balance threshold, make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` opt-in (e.g., only on `_mint`, not `transfer`, or via `setAccountCollateral` flags), or allow overflow eviction / raise `MAX_TOKENS_PER_USER`. Alternatively, let recipients clear entries cheaply without a transfer (a `removeFromDepositTokensOfAccount` callable by the account itself when balance is zero is currently impossible since only the token contract may call it).

### Proof of Concept
Hardhat/fork outline:
1. On a pool with `k` deposit tokens, attacker deposits minimal underlying in each, obtaining `1 wei`-scale `DepositToken` balances.
2. For each token `T`: `T.transfer(victim, 1)` → victim's `depositTokensOfAccount` grows; repeat with victim's existing debt tokens counted toward 30.
3. `expect(T_new.deposit(amount, victim)).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens")` and `debtToken.issue`/`mint` for a new synthetic also reverts.
4. For the feeCollector variant: set `depositFee > 0` via `FeeProvider`, fill the collector's list, then `deposit` on a token the collector has zero balance of reverts inside `_mint(feeCollector, _fee)`.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
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

**File:** contracts/DepositToken.sol (L225-237)
```text
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

**File:** contracts/DepositToken.sol (L348-354)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
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

**File:** contracts/DepositToken.sol (L545-551)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);
```
