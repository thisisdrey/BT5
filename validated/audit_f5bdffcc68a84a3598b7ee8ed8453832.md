### Title
Dust-transfer griefing fills a victim's `MAX_TOKENS_PER_USER` slots, permanently blocking their deposits of new collateral and issuance of new debt positions - (File: contracts/Pool.sol)

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` across `depositTokensOfAccount` + `debtTokensOfAccount` via `onlyIfAdditionWillNotReachMaxTokens` [1](#0-0) [2](#0-1) . Any holder of a `DepositToken` (msdTOKEN) can `transfer()` an arbitrarily small amount to any address; `DepositToken._transfer` then calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance was zero, consuming one slot [3](#0-2) . An unprivileged attacker can therefore deposit dust into every deposit token of a pool and transfer 1 wei of each msdTOKEN to a victim, filling their 30 slots and causing every subsequent `deposit(amount, onBehalfOf=victim)`, `issue`, `mint`, or inbound `transfer`/`seize` of a *new* token type to revert with `UserReachedMaxTokens` [4](#0-3) [5](#0-4) .

### Finding Description
- Entry point (attacker): `DepositToken.transfer(victim, 1)` for each of the pool's deposit tokens after making dust deposits via `DepositToken.deposit(amount_, attacker)` [6](#0-5) .
- `transfer()` only checks `_revertIfLocked(msg.sender, amount_)`, which passes for any account with no debt [7](#0-6) [8](#0-7) .
- Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) >= 30`, `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert [2](#0-1) .
- Blocked victim flows: `deposit()` (via `_mint` → `addToDepositTokensOfAccount`), `issue()`/`mint()` (debt token add in `_mint`), `DebtToken.issue` in `SmartFarmingManager` leverage paths, receipt of any new msdTOKEN via `transfer`/`seize`. The `DepositTokenAlreadyExists` revert additionally makes the attack revert-proof on re-adds only after cleanup.
- The attacker can front-run or continuously re-grief: after the victim clears a slot by transferring dust out (balance → 0 triggers `removeFromDepositTokensOfAccount` [9](#0-8) ), the attacker re-sends dust.

### Impact Explanation
Partial denial of service matching the CVE bug class: an unauthenticated network attacker temporarily freezes the victim's ability to open or extend positions. Concretely:
- The victim cannot deposit *new* collateral types and cannot mint *new* synthetic debt — including the case where they must add a different collateral to restore health. A position drifting toward liquidation cannot be rescued with a new collateral type while the cap is saturated, enabling forced liquidation.
- Third-party `deposit(amount_, onBehalfOf_ = victim)` (e.g., gateways, zaps) and liquidator `seize` into fresh tokens revert.
- The victim can self-recover only by spending gas to transfer out each dust token, and the attack can be repeated cheaply, making the freeze effectively persistent.

### Likelihood Explanation
- Fully permissionless: requires only EOA calls to public `deposit`/`transfer`. No privileged role, oracle manipulation, or malicious endpoint needed.
- Cost: dust collateral (recoverable via `withdraw`) plus gas for ~30 transfers — cheap relative to the blocking power.
- No modifier prevents it: `whenNotPaused`, `nonReentrant`, SynthContext checks, and health checks are all orthogonal; `transfer` has no recipient-consent mechanism.
- Caveat: impact is bounded — withdrawals of existing balances still work, so it is a temporary/partial freeze rather than permanent loss; severity is medium, consistent with the reference advisory's `A:L` rating. I could not fully verify against prior audit reports whether this exact griefing was already disclosed; if it appears in a known-issues list it should be rejected on that basis.

### Recommendation
Do not revert on cap saturation for inbound `transfer`/`seize`/deposit-mint paths, or decouple balance tracking from the capped enumerable set. Options: allow `addToDepositTokensOfAccount` to silently skip (return bool) once at the cap so dust can't be weaponized (list used only for `debtPositionOf`/`depositOf` would then undercount — a tradeoff); or require recipient opt-in (e.g., only add via `deposit`/`issue`, and index by scanning the global `depositTokens` set); or revert only when the *account itself* initiates the action. At minimum, document that `transfer` grants no consent and let users clear slots cheaply (e.g., a `sweepDust` helper that transfers out zero-balance leftovers).

### Proof of Concept
Hardhat sketch (fork or local deploy with N deposit tokens):

```ts
// attacker deposits dust in every deposit token of the pool
for (const dt of depositTokens) {
  await underlying(dt).approve(dt.address, DUST)
  await dt.connect(attacker).deposit(DUST, attacker.address)
  // transfer 1 wei of msdTOKEN to victim -> consumes one slot
  await dt.connect(attacker).transfer(victim.address, 1)
}

// victim already had some positions; after attacker fills remaining slots:
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30 - debtLen)

// victim's deposit of a NEW collateral type reverts
await expect(newDepositToken.connect(victim).deposit(amt, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim's issuance of a NEW synthetic debt reverts (debt token add)
await expect(newDebtToken.connect(victim).issue(victim.address, amt))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// liquidator seize of a token type the liquidator's set is also full on reverts
await expect(pool.connect(liquidator).liquidate(...))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

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

**File:** contracts/DepositToken.sol (L343-354)
```text
    function seize(address from_, address to_, uint256 amount_) external override onlyIfCanSeize {
        _transfer(from_, to_, amount_);
    }

    /// @inheritdoc IERC20
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
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

**File:** contracts/DepositToken.sol (L506-525)
```text
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
