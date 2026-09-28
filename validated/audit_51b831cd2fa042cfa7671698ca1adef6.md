### Title
Dust-transfer griefing fills a victim's token list to `MAX_TOKENS_PER_USER`, permanently blocking them from depositing new collateral or issuing new debt - (File: contracts/Pool.sol)

### Summary
`Pool` keeps a per-account list of deposit and debt tokens capped at `MAX_TOKENS_PER_USER = 30`. Entries are added to `depositTokensOfAccount` as a side effect of `DepositToken._transfer` whenever a recipient's balance goes `0 -> >0`, and the add reverts with `UserReachedMaxTokens` once the combined count reaches 30. An unprivileged attacker can dust-transfer small amounts of every `DepositToken` (and mint dust of every `DebtToken` via `issue`) to a victim, permanently filling the victim's list and making all subsequent deposits of new collateral types and issuances of new synthetic debt revert.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30` [1](#0-0) 
- `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` apply that modifier and are callable only by registered deposit/debt tokens [2](#0-1) 
- `DepositToken._transfer` adds the token to the recipient's list whenever `_recipientBalanceBefore == 0 && amount_ > 0`; the sender only needs an unlocked balance [3](#0-2) 
- `DepositToken._mint` (reached via `deposit(amount_, onBehalfOf_)` or `deposit(1, victim)`) performs the same add, so anyone can also push entries by depositing dust `onBehalfOf_` the victim [4](#0-3) 
- `DebtToken.issue`/`mint` similarly calls `pool.addToDebtTokensOfAccount(account_)` for first-time holders, so the attacker can issue dust debt to the victim or transfer synthetic-token-backed debt positions to fill the remaining slots.

Attack: for each of the pool's deposit tokens, the attacker deposits a minimal amount of underlying and calls `depositToken.transfer(victim, 1 wei)` (or `deposit(1, victim)`). Each call permanently consumes one of the victim's 30 slots; the victim cannot remove an entry without zeroing that balance, and even then the attacker re-adds it with another dust transfer for near-zero cost. Once the count hits 30, every `deposit()` of a collateral type the victim doesn't already hold reverts, and every `issue()` of a synthetic the victim doesn't already owe reverts.

### Impact Explanation
The victim is frozen out of adding any new collateral or debt position. For a victim with an open unhealthy (or soon-unhealthy) position whose existing collateral type can't be topped up (e.g., they hold none of it off-chain), the inability to deposit any *new* collateral type means the position cannot be rescued and is forcibly liquidated — a direct loss caused by the blocked deposit path. More generally, all deposits/mints to the victim from third parties (zaps, SmartFarmingManager leverage, cross-chain `PT_SEND_AND_CALL` credit ops that mint deposit/debt tokens to the account) revert, permanently bricking that address's use of the protocol until entries are freed — matching the availability/DoS class of the reference bug (a specific crafted setting causes the system to crash on activation).

### Likelihood Explanation
- Fully unprivileged: requires only `DepositToken.transfer`/`deposit` (and `DebtToken.issue`) with dust amounts; cost is bounded by a few units of each underlying.
- No governance, oracle, or privileged precondition; works on the deployed configuration since `MAX_TOKENS_PER_USER` is a constant and the modifier has no escape hatch for the victim.
- Re-adding after removal is trivial and cheap, so the victim cannot reliably self-heal.
- Limitation: impact is DoS/forced-liquidation of one target at a time; the attacker gains nothing directly, so likelihood is moderate rather than high.

### Recommendation
- Only add to `depositTokensOfAccount`/`debtTokensOfAccount` when the account actively calls `deposit`/`issue` for itself, or gate list insertion behind the account's own action rather than any incoming transfer.
- Alternatively, allow removal of any entry whose balance is dust/zero and make `transfer`-driven additions not count toward `MAX_TOKENS_PER_USER`, or increase the cap and skip the check for transfers below a threshold.

### Proof of Concept
Foundry fork outline:

```solidity
// Pool pool; DepositToken[] dts; DebtToken[] debts; address victim;
// Victim has an open position in dts[0]/debts[0] only.

function test_DustGriefLocksVictim() public {
    address attacker = makeAddr("attacker");

    for (uint i = 1; i < dts.length && i < 30; ++i) {
        // attacker deposits dust on behalf of victim (or transfer 1 wei)
        IERC20 underlying = dts[i].underlying();
        deal(address(underlying), attacker, 1e6);
        vm.startPrank(attacker);
        underlying.approve(address(dts[i]), 1);
        dts[i].deposit(1, victim); // victim list grows by 1
        vm.stopPrank();
    }
    // fill remaining slots via debt-token issuance to victim
    // ...

    assertEq(
        pool.getDepositTokensOfAccount(victim).length +
        pool.getDebtTokensOfAccount(victim).length,
        pool.MAX_TOKENS_PER_USER()
    );

    // victim (or anyone) can no longer deposit a new collateral type to victim
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    dtsNew.deposit(1e18, victim); // reverts even via transfer/seize/SmartFarming

    // victim cannot issue new synthetic debt either
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    debtsNew.issue(victim, 1e18);
}
```

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

**File:** contracts/DepositToken.sol (L486-488)
```text
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
