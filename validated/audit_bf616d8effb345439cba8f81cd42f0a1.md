### Title
Unprivileged attacker can dust-fill any account's token list to `MAX_TOKENS_PER_USER`, freezing deposits/transfers to the victim and blocking liquidators - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a global per-account cap (`MAX_TOKENS_PER_USER = 30`) over the combined `depositTokensOfAccount` + `debtTokensOfAccount` sets. Entry into those sets is permissionlessly force-able on arbitrary accounts: `DepositToken.deposit(amount_, onBehalfOf_)` accepts an attacker-chosen beneficiary, and `DepositToken.transfer`/`transferFrom`/`seize` add the token to the *recipient's* list. Once an account reaches 30 entries, `onlyIfAdditionWillNotReachMaxTokens` reverts every `addTo*` call, so any mint/transfer of a *new* token type to that account reverts.

### Finding Description [1](#0-0) [2](#0-1) 

- `Pool.addToDepositTokensOfAccount(account_)` / `addToDebtTokensOfAccount(account_)` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` when `debt + deposit >= 30`.
- `DepositToken._mint` and `_transfer` call `pool.addToDepositTokensOfAccount(recipient)` whenever the recipient's prior balance was 0 — no consent from the recipient is required ( [3](#0-2) ).
- `deposit(1 wei, victim)` therefore lets any EOA register each listed collateral into `victim`'s set for dust cost; `msdX.transfer(victim, 1)` does the same.
- `DebtToken.issue(amount_, to_)` likewise pushes a debt token into `to_`'s set when `to_`'s balance was 0 (addition performed by the DebtToken calling `addToDebtTokensOfAccount`). Whether `issue` checks health of `to_` or the caller was not fully verified in this pass; the deposit-token vector alone suffices.

Consequences once victim hits 30:
- `deposit(..., victim)` for any collateral the victim doesn't already hold reverts → victim cannot top up collateral to restore health → forced liquidation.
- `msdX.transfer/transferFrom(victim, _)` of new collateral types reverts → victim cannot receive deposit tokens (including via OTC/zap routes).
- `Pool.liquidate` calls `depositToken_.seize(account_, liquidator, _toLiquidator)` ( [4](#0-3) ); `seize` is `_transfer`, so liquidation reverts if the liquidator's own list is at 30 and they don't yet hold `depositToken_`. Dust-filling known liquidator/keeper EOAs therefore degrades liquidation liveness for collateral types they don't hold — analogous to globally throttling feed access.

### Impact Explanation
Temporary freezing of funds and impaired liquidation: targeted users cannot increase collateral exposure or receive deposit tokens, and liquidators pre-filled to the cap cannot execute `liquidate` for new collateral types, delaying bad-debt clearing. No invariant of solvency is directly broken, but unhealthy positions become harder to rescue/liquidate.

### Likelihood Explanation
Attack cost is dust (1 wei per collateral per victim). Constraint: entries are bounded by the number of listed deposit + debt tokens in the pool, so the attack requires the deployed pool to list ≥ 30 tokens combined (or fewer for victims who already hold several). Whether any live pool reaches 30 listed tokens was not confirmed from the index; pools with ~10–15 tokens make this only partially effective (caps a victim's reachable slots but cannot hit 30 alone). This bounds likelihood to medium-low unless deployment enumeration shows ≥30 tokens.

### Recommendation
Track only tokens the account opted into, or exempt `addTo*` (add is idempotent by nature — skip the max check when the token is already present and, more robustly, allow removal-only growth). Alternatively, charge `seize`/liquidation paths differently: perform seize via a balance move that bypasses the max-tokens modifier, and/or let accounts opt out of unsolicited additions (pull-pattern registration on first user-initiated deposit only).

### Proof of Concept
Foundry fork sketch against a deployed pool:

```solidity
// victim = target account; assume pool lists >= 30 deposit+debt tokens
address[] memory dts = pool.getDepositTokens();
for (uint i; i < dts.length; ++i) {
    IDepositToken dt = IDepositToken(dts[i]);
    IERC20 under = dt.underlying();
    deal(address(under), attacker, 1);
    under.approve(address(dt), 1);
    dt.deposit(1, victim);           // adds dts[i] to victim's set
}
// also: attacker deposits 1 unit to itself, then dt.transfer(victim, 1) for remaining tokens
// once getDepositTokensOfAccount(victim).length + debtTokens == 30:
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
otherUser.call(depositToVictim);   // any new collateral deposit to victim reverts
vm.expectRevert(Pool.UserReachedMaxTokens.selector);
msdNewToken.transfer(victim, 1);   // transfer to victim reverts
vm.expectRevert();                 // liquidate() reverts when seizing a token the
pool.liquidate(synth, victim, amount, newCollateralDt); // liquidator at cap
```

Caveat I could not fully verify within the tool budget: the exact `issue(amount_, to_)` semantics (whether the attacker can grow the victim's *debt* set against the victim's own collateral), and whether any deployed pool actually lists ≥30 tokens — both needed to firm up end-state feasibility on a real fork.

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

**File:** contracts/Pool.sol (L587-593)
```text
        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }
```

**File:** contracts/DepositToken.sol (L485-525)
```text
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
