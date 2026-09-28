### Title
Unprivileged dust transfers fill a victim's `MAX_TOKENS_PER_USER` list and DoS all new deposits/borrows - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The Java advisory is an unauthenticated, network-reachable *partial denial of service* (availability-only impact). The strongest reachable Metronome analog is the per-account token-list cap (`MAX_TOKENS_PER_USER = 30`) in `Pool`, which is enforced on behalf of any account that *receives* deposit tokens. Because `DepositToken._transfer` unconditionally registers the recipient in `depositTokensOfAccount` the first time they hold a token, any EOA can force-fill a victim's list with dust amounts of every listed deposit token, after which the victim cannot deposit a new collateral type, cannot issue a new debt type, and cannot receive seized collateral or deposit tokens — all transactions revert with `UserReachedMaxTokens`.

### Finding Description
`Pool` tracks per-account collateral/debt positions in `MappedEnumerableSet` lists (`depositTokensOfAccount`, `debtTokensOfAccount`), capped at `MAX_TOKENS_PER_USER = 30` [1](#0-0) . The cap is enforced by `onlyIfAdditionWillNotReachMaxTokens`, which counts debt + deposit entries together [2](#0-1) . Both `addToDebtTokensOfAccount` and `addToDepositTokensOfAccount` are gated by it [3](#0-2) .

The problem is that insertion is triggered by *receiving* tokens, not by any action of the account owner. `DepositToken._transfer` adds the recipient to the list whenever their prior balance was zero [4](#0-3) , and `_mint` does the same for `deposit()` beneficiaries [5](#0-4) . `transfer`/`transferFrom` are public and permissionless, guarded only by `_revertIfLocked` on the *sender* [6](#0-5) .

Attack path:
1. Attacker deposits a tiny amount into each of the pool's N listed deposit tokens (pool caps deposit tokens at `MAX_TOKENS_PER_USER` too, see `addDepositToken` [7](#0-6) ).
2. Attacker calls `depositToken_i.transfer(victim, dust)` for each token. Each call hits `addToDepositTokensOfAccount(victim)` since the victim's balance is 0 → >0.
3. Victim's `depositTokensOfAccount` now has N entries. Any subsequent `deposit()`, `DebtToken.issue()`, `Pool.swap()` minting a new debt position, `SmartFarmingManager.leverage`, or `Pool.liquidate`/`seize` that would add a *new* token reverts in `onlyIfAdditionWillNotReachMaxTokens`.

Cleanup is unreliable: if the victim has open debt, the dust balance is counted as collateral and `unlockedBalanceOf` can return less than the dust balance, making it locked/unremovable via `_revertIfLocked` [8](#0-7) [9](#0-8) . Even when removable, the attack is repeatable at negligible cost after the one-time dust deposit, allowing indefinite front-running re-griefing.

### Impact Explanation
Availability-only, mirroring the CVE's partial-DoS class. A targeted victim (e.g., a leveraged SmartFarming position or an account approaching liquidation) is blocked from opening any *new* collateral or debt position, and a liquidator who already holds 30 tracked tokens cannot receive seized collateral (`seize` → `_transfer` → `addToDepositTokensOfAccount` reverts) [10](#0-9) . For a victim whose dust is locked by existing debt, the slots cannot be freed, so the freeze persists for the life of the debt — a temporary freezing of funds/positions under the accepted impact classes. Existing-token deposits, withdrawals, and repayments still work, which keeps this at Medium severity rather than a full fund freeze.

### Likelihood Explanation
Fully permissionless: the attacker needs only underlying tokens for dust deposits and gas. No privileged role, oracle manipulation, or timing dependence. Cost scales with the number of listed deposit tokens (≤30) and is bounded by dust amounts. The main mitigations — victim can pre-clear slots or top up already-held collaterals — reduce but do not eliminate the DoS, particularly against indebted accounts where dust is locked.

### Recommendation
Do not let inbound transfers consume a recipient's capped slots: either (a) only register the account in `depositTokensOfAccount` on `deposit()`/`_mint` (position creation) and compute `depositOf` from actual balances, or (b) exempt transfers below a minimum threshold, or (c) drop entries on transfer-out even when a dust balance remains. Alternatively, only revert in the cap check when the sender is the position owner, and let forced recipient entries overflow the list without blocking the victim's own actions.

### Proof of Concept
Hardhat fork sketch (deployed `Pool`, listed `DepositToken`s):

```ts
// pool, depositTokens[0..n] attached at deployed addresses; attacker is an EOA.
const victim = await ethers.getSigner(VICTIM);
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30

for (let i = 0; i < n && i < max; i++) {
  const dt = depositTokens[i];
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  // attacker acquires 1 wei of underlying (swap/whale transfer on fork)
  await underlying.approve(dt.address, 1);
  await dt.deposit(1, attacker.address);
  await dt.transfer(victim.address, 1); // registers victim -> depositTokensOfAccount
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(n);

// Any NEW-token deposit or issue for victim now reverts:
await expect(
  depositTokens[n - n].connect(victimSigner).deposit(amount, victim.address) // fresh token type
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Liquidator holding 30 tracked tokens cannot seize:
await expect(
  pool.connect(liquidator).liquidate(victim.address, synth.address, amount, depositTokenX.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Caveats not fully verified in this pass: whether `DebtToken` balances are non-transferable (which would cap attacker-forced entries at the number of listed deposit tokens rather than 30 combined) and whether `seize`/liquidation paths hit the same `addToDepositTokensOfAccount` guard on all deployed versions. Both affect only the magnitude of the DoS, not its existence.

### Citations

**File:** contracts/Pool.sol (L76-79)
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

**File:** contracts/Pool.sol (L703-709)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();

        depositTokenOf[_underlying] = IDepositToken(depositToken_);

        emit DepositTokenAdded(depositToken_);
```

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L343-345)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }
```

**File:** contracts/DepositToken.sol (L348-376)
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

        return true;
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
