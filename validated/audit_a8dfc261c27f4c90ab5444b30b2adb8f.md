### Title
Unprivileged attacker permanently DoSes a victim's ability to add new collateral/debt tokens by filling their 30-slot account list via dust `deposit`/`transfer` - (File: contracts/Pool.sol, contracts/DepositToken.sol)

### Summary
The MySQL CVE is a post-auth availability bug (forced hang/crash). The Metronome analog is a liveness/availability break reachable by any EOA: `Pool` enforces `MAX_TOKENS_PER_USER = 30` across `debtTokensOfAccount + depositTokensOfAccount`, and reverts `UserReachedMaxTokens` on any addition once the list is full (`Pool.sol:143-148`). [1](#0-0)  Because `DepositToken.deposit(amount_, onBehalfOf_)` accepts an arbitrary beneficiary with no health check or consent, and `DepositToken._mint`/`_transfer` unconditionally register the token on the recipient (`DepositToken.sol:486-488`, `517-520`), an attacker can dust-fill a victim's list to 30, permanently reverting every future first-time deposit token mint and every first-time debt issuance for that account.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when the combined per-account list reaches 30 (`Pool.sol:143-148`, `204-208`, `216-220`). [2](#0-1) 
- `DepositToken.deposit` mints to `onBehalfOf_` chosen by the caller; the only checks are non-zero amount/beneficiary and that the caller isn't the Treasury (`DepositToken.sol:211-237`). [3](#0-2) 
- `_mint` and `_transfer` call `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance was 0 (`DepositToken.sol:485-488`, `517-520`). [4](#0-3) 
- Entries are only removed when the balance goes fully to zero (`_burn` line 460-462, `_transfer` line 523-525). If the victim has outstanding debt, `transfer`/`withdraw` are blocked by `_revertIfLocked` → `unlockedBalanceOf` returns 0 or less than the dust balance (`DepositToken.sol:180-182`, `348-354`, `383-398`, `406-412`), so the victim **cannot evict the attacker-inserted tokens**. [5](#0-4) 
- Consequence: once the list is full, `DebtToken._mint`'s `pool.addToDebtTokensOfAccount(account_)` reverts (`DebtToken.sol:597-600`), so `DebtToken.issue` always reverts for the victim — they can never borrow a new synthetic asset; likewise any new collateral deposit reverts. A victim who is near liquidation cannot onboard a new collateral type to restore health, and cannot fully unlock/withdraw the dusted tokens while debt exists — the block is permanent for that account state. [6](#0-5) 

### Impact Explanation
Availability loss matching the CVE class (complete DoS of a function, not theft): the victim's account is bricked for all new collateral types and all new synthetic-asset issuance. For an account carrying debt, the dust cannot be removed (locked by `_revertIfLocked`), making the freeze permanent until the debt is fully repaid — and repaying requires synths the victim may need to acquire by issuing, which is itself blocked for new debt tokens. If the victim needs a new collateral type to avoid liquidation, liquidation becomes unavoidable, converting the liveness break into a forced loss.

### Likelihood Explanation
Fully unprivileged: the attacker only needs to call `DepositToken.deposit(dustAmount, victim)` (or `transfer`) once per registered deposit token — no privileged role, no oracle manipulation, no governance. Cost is bounded by dust amounts of underlying for each listed deposit token plus gas. Feasibility requires the pool to have enough registered deposit/debt tokens to reach 30 entries on the victim (the pool-level deposit token list is also capped by `ReachedMaxDepositTokens`), so impact scales with the number of listed collaterals on the deployed pools; on pools with few collaterals the attacker can only partially fill the list. No pause flag, reentrancy guard, or SynthContext modifier mitigates it, since the calls are ordinary deposits/transfers.

### Recommendation
Track token membership per account with a cap only counting tokens the account opted into, or exempt entries added via `deposit` on behalf of others; alternatively (a) allow removal of zero-locked/dust entries regardless of locked balance (e.g., let `transfer`/`withdraw` skip `_revertIfLocked` for balances the account never deposited), (b) auto-eject the smallest/dust deposit tokens when at cap, or (c) only add to `depositTokensOfAccount` on `deposit`/`seize` (position-creating paths) and not on plain `transfer` of dust.

### Proof of Concept
Hardhat fork/test sketch:

```ts
// victim has an open debt position (debtToken principalOf[victim] > 0)
// pool lists depositTokens d0..dN (N chosen so victim reaches MAX_TOKENS_PER_USER=30)
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, dust)
  await dt.connect(attacker).deposit(dust, victim.address)   // adds dt to victim's list
}
// list now == 30 -> every new token addition reverts
await expect(newDepositToken.connect(victim).deposit(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(newDebtToken.connect(victim).issue(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// victim cannot evict dusted tokens: unlockedBalanceOf(victim) < dust while debt > 0
await expect(d0.connect(victim).transfer(attacker.address, dust))
  .to.be.revertedWithCustomError(d0, 'NotEnoughFreeBalance')
```

Reproduce against a forked pool deployment (e.g., `deployments/mainnet/Pool.json`) using real registered `DepositToken`/`DebtToken` addresses and dust deposits of each underlying.

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

**File:** contracts/DepositToken.sol (L485-489)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DebtToken.sol (L597-601)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
    }
```
