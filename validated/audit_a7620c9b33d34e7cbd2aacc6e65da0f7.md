### Title
Unprivileged attacker can dust-fill a victim's per-account token list (`MAX_TOKENS_PER_USER`) to block deposits, transfers and debt issuance — ([File: contracts/Pool.sol](contracts/Pool.sol) / [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`Pool` tracks, per account, which `DepositToken`s and `DebtToken`s have a non-zero balance, in `depositTokensOfAccount` / `debtTokensOfAccount` (`MappedEnumerableSet`). Any balance transition 0 → >0 triggers `pool.addToDepositTokensOfAccount(account_)` / `addToDebtTokensOfAccount(account_)`, which revert with `UserReachedMaxTokens` once the combined count reaches `MAX_TOKENS_PER_USER = 30`. Since `DepositToken.transfer`/`transferFrom` and `deposit(amount_, onBehalfOf_)` are permissionless and accept any recipient, an attacker can push dust of every listed deposit token into a victim's list, permanently gating every "first-time" addition for that account behind a revert.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [1](#0-0) [2](#0-1) .
- `DepositToken._transfer` (and `_mint` via `deposit(amount_, onBehalfOf_)`) unconditionally adds the token to the *recipient's* list when their balance was zero [3](#0-2) [4](#0-3) .
- `Pool.addDepositToken` allows up to `MAX_TOKENS_PER_USER` (30) deposit tokens per pool [5](#0-4) , so an attacker alone can occupy all 30 slots of a victim's list using only deposit tokens.
- `DebtToken` is non-transferable and debt additions only happen through `_mint` on `issue`/`mint`/`flashIssue` [6](#0-5) , meaning once the victim's list is full, their *first* issuance of every debt token reverts inside `addToDebtTokensOfAccount`.

Attack trace (all public, unprivileged):
1. Attacker deposits (or acquires) a dust amount of each of the pool's deposit tokens.
2. For each deposit token `msdX`, attacker calls `msdX.transfer(victim, 1)` or `msdX.deposit(1 wei underlying, victim)`. Each call hits `Pool.addToDepositTokensOfAccount(victim)` [4](#0-3) .
3. After 30 fills, `victim`'s list is at capacity. Thereafter:
   - `msdNewToken.deposit(..., victim)` / `transfer(victim, ...)` for any token the victim doesn't already hold → revert `UserReachedMaxTokens`.
   - `debtToken.issue(amount, to)` where victim has no existing balance in that debt token → `debtPositionOf` check passes, `_mint` reverts in `addToDebtTokensOfAccount` [7](#0-6) [6](#0-5)  — victim cannot open debt in a new synthetic.
   - Third parties cannot send the victim any new deposit token (transfers, liquidation `seize` payouts, deposits on their behalf all revert).

### Impact Explanation
- Denial of service of core protocol entry points for the targeted account: depositing new collateral types, receiving deposit tokens, and issuing any new synthetic debt all revert. The victim cannot top up with a different collateral or open a new debt position, including during a pending liquidation where adding collateral is the only rescue path.
- The freeze is temporary rather than permanent only in the sense that the victim can remove entries by fully transferring each dust balance out (`_transfer` removes the entry on balance → 0 [8](#0-7) ), but each cleanup transaction can be trivially re-griefed by the attacker front-running/re-dusting, and locked-balance accounting (`unlockedBalanceOf`) constrains transfers when the victim carries debt.
- Cost to the attacker is bounded by dust across ≤30 tokens; impact persists as long as the attacker maintains it. This matches the accepted "temporary freezing of funds / blocked protocol liveness for a victim account" class.

### Likelihood Explanation
- Fully permissionless: requires only an EOA, dust amounts of listed underlying/deposit tokens, and public `transfer`/`deposit` calls. No privileged role, oracle manipulation, or governance action needed.
- Requires the victim to have fewer than 30 tracked tokens; trivially true for ordinary accounts. Cost scales with the number of whitelisted deposit tokens actually deployed (fewer needed if the pool lists <30, since debt-token additions share the same counter).
- Mitigations already present do not stop it: `nonReentrant`, `whenNotPaused`, `SynthContext._msgSender`, `onlyIfDepositTokenExists`, and `_revertIfLocked` all pass for the attacker's own transfers; the quota check itself is the griefing vector.

### Recommendation
- Do not let arbitrary inbound transfers mint list entries for the recipient. Track a token in `depositTokensOfAccount` only via `deposit(..., onBehalfOf_)`-style flows initiated for the account, or make inbound `transfer`/`seize` skip list insertion (compute collateral value lazily, or keep a separate "deposited" flag vs. "received" flag).
- Alternatively, make `addTo*TokensOfAccount` non-reverting on overflow (best-effort add, or skip-and-continue for unsolicited transfers) so a third party cannot force a revert into the victim's future operations; ensure `debtPositionOf`/`depositOf` still account for untracked balances.
- At minimum, exempt `seize`/liquidation-credit paths and give the victim a cheap batched "removeDustEntry" escape that cannot be re-griefed within the same transaction.

### Proof of Concept
Hardhat fork sketch (pool `P` with deposit tokens `msdA…msdN`):

```ts
// setup: victim has 0 balances everywhere
for (const msd of depositTokens) {           // up to 30 tokens
  const underlying = await ethers.getContractAt('IERC20', await msd.underlying())
  // attacker gets dust underlying and deposits on behalf of victim (or transfers msd dust)
  await underlying.connect(attacker).approve(msd.address, dustAmount)
  await msd.connect(attacker).deposit(dustAmount, victim.address) // adds to victim's list
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30)

// 1) victim cannot receive a new deposit token type / deposit a new collateral
const newMsd = /* newly added deposit token */;
await expect(newMsd.deposit(parseEther('1'), victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(msdA.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens') // if victim emptied msdA... use any token they don't hold

// 2) victim cannot issue debt in any synthetic (first-time debt token add reverts)
await expect(debtToken.connect(victim).issue(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Cleanup cost check: `victim` transferring out a dust balance fully removes that entry (`_transfer` → `removeFromDepositTokensOfAccount`), confirming the freeze is temporary but re-griefable, not permanent.

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

**File:** contracts/Pool.sol (L698-705)
```text
    function addDepositToken(address depositToken_) external onlyGovernor {
        if (depositToken_ == address(0)) revert AddressIsNull();
        IERC20 _underlying = IDepositToken(depositToken_).underlying();
        if (address(depositTokenOf[_underlying]) != address(0)) revert UnderlyingAssetInUse();
        // Note: Fee collector collects deposit tokens as fee
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```

**File:** contracts/DepositToken.sol (L522-525)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```

**File:** contracts/DebtToken.sol (L235-271)
```text
    function issue(
        uint256 amount_,
        address to_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _issued, uint256 _fee)
    {
        if (amount_ == 0) revert AmountIsZero();

        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (, , , , uint256 _issuableInUsd) = _pool.debtPositionOf(_msgSender);

        IMasterOracle _masterOracle = _pool.masterOracle();

        if (amount_ > _masterOracle.quoteUsdToToken(address(_syntheticToken), _issuableInUsd)) {
            revert NotEnoughCollateral();
        }

        _mint(_pool, _masterOracle, _msgSender, amount_);

        (_issued, _fee) = quoteIssueOut(amount_);
        if (_fee > 0) {
            _syntheticToken.mint(_pool.feeCollector(), _fee);
        }
        _syntheticToken.mint(to_, _issued);

        emit SyntheticTokenIssued(_msgSender, to_, amount_, _issued, _fee);
    }
```

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
