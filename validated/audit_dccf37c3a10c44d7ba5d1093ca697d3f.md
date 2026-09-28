### Title
Attacker can fill an account's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER` with dust transfers, permanently DoS-ing deposits/withdrawals/liquidation fee mints for any new collateral (including freezing withdrawals when the victim is the `feeCollector`) - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to CVE-2018-7052 (crash when the number of windows exceeds available space), `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once `debtTokensOfAccount + depositTokensOfAccount` hits `MAX_TOKENS_PER_USER = 30`. Because `DepositToken._transfer` (called by `transfer`, `transferFrom`, and `seize`) calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to non-zero, any unprivileged attacker can push 1 wei of many distinct deposit tokens to a victim (or to the pool's `feeCollector`) and permanently occupy all 30 slots. After that, every code path that would add a *new* token to that account's list reverts. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Finding Description
- `DepositToken._transfer` adds the token to `recipient_`'s list on first receipt (`_recipientBalanceBefore == 0 && amount_ > 0`). There is no minimum amount and no opt-in; a mere `transfer(victim, 1)` suffices per token.
- Once the combined list reaches 30 entries, `addToDepositTokensOfAccount` reverts, which poisons: `DepositToken.deposit`/`_mint` for a new collateral to that account, `DepositToken.transfer/transferFrom`/`seize` of any new collateral to that account, and `DebtToken._mint`/`issue` for a new debt token (same modifier on `addToDebtTokensOfAccount`).
- The most severe target is `feeCollector`: `DepositToken._mint(feeCollector, fee)` runs inside every `deposit` with `depositFee > 0`, and `_withdraw` performs `_transfer(account, feeCollector, fee)` on every `withdraw`/`flashWithdraw`/`withdrawFrom` with `withdrawFee > 0`. Dust-filling the feeCollector to 30 slots therefore bricks all deposits and withdrawals (and `seize` fee transfers inside `Pool.liquidate`) for any collateral the feeCollector hasn't touched yet — a pool-wide DoS for those assets.
- `unlockedBalanceOf` returns 0 for an unhealthy/debt-locked victim (`_issuableInUsd == 0`), so such a victim cannot even transfer the dust back out to free a slot; their only recovery is repaying debt or topping up an already-listed collateral. [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) 

### Impact Explanation
Liveness / freezing of funds. For a targeted user: inability to deposit additional collateral types or mint new debt positions, which can force a marginal position into liquidation they cannot defend against. For `feeCollector`: all `deposit` calls with a fee revert for collateral tokens not already in its list, and all `withdraw*` calls with a `withdrawFee > 0` revert for the same — user funds are frozen in those DepositTokens until governance sets fees to 0 or the feeCollector contract (if it even can) sweeps/transfer the dust. Since `feeCollector` is a protocol-controlled address that may lack arbitrary-token transfer capability, the freeze can be effectively permanent for affected tokens.

### Likelihood Explanation
Requires an unprivileged EOA only: deposit dust into each whitelisted collateral, then `transfer(victim, 1)` for each. Cost is gas plus trivial dust amounts. Feasibility depends on the deployed pool having enough whitelisted deposit+debt tokens to reach 30 combined entries (bounded by `ReachedMaxDepositTokens` and the number of synthetic offerings); on pools with ≥30 listed instruments the attack is fully permissionless and cannot be blocked by the victim. No privileged role, oracle manipulation, or flash loan is needed.

### Recommendation
- Do not add a token to `depositTokensOfAccount` on `seize`/fee transfers for `feeCollector`, or exempt `feeCollector` (and `treasury`) from the per-account accounting.
- Apply a meaningful minimum threshold (or governance opt-in) before a first-time balance is registered, so 1-wei dust cannot occupy a slot.
- Allow `seize`/liquidation fee transfers and `deposit` mints to skip the max-tokens check (e.g., don't count protocol fee balances), or make the check apply only to debt positions rather than the combined list.
- Provide a way for an account to prune entries with zero/insignificant balance.

### Proof of Concept
Hardhat sketch (pool with N≥30 listed deposit tokens `dt[i]`, victim = `feeCollector`):

```ts
// attacker deposits dust into each collateral and transfers 1 wei to feeCollector
const feeCollector = await pool.feeCollector();
for (const dt of depositTokens) { // every whitelisted DepositToken
  await underlying[dt].approve(dt.address, dust);
  await dt.deposit(dust, attacker.address);           // mint attacker balance
  await dt.transfer(feeCollector, 1);                 // adds dt to feeCollector's list
}
// now depositTokensOfAccount[feeCollector].length == 30 (with any debt tokens counted too)

// any deposit into a collateral the feeCollector has never held now reverts:
const newDepositToken = await addNewCollateral();     // freshly whitelisted DepositToken
await expect(newDepositToken.deposit(amount, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
// same for withdraw (fee _transfer to feeCollector) and liquidate (seize fee split):
await expect(existingDt.withdraw(amount, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens'); // if withdrawFee > 0 and token not in list
```

For a user victim: fill `victim`'s list with 30 dust entries, then `victim.deposit` into any new collateral or `debtToken.issue` for a new synthetic reverts with `UserReachedMaxTokens`; if `victim`'s position is unhealthy (`unlockedBalanceOf == 0`), the dust cannot be transferred out, so the griefing persists until debt is repaid or an existing collateral is topped up.

### Citations

**File:** contracts/Pool.sol (L79-79)
```text
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

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
    }
```

**File:** contracts/DepositToken.sol (L225-236)
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

**File:** contracts/DepositToken.sol (L545-553)
```text
        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
```
