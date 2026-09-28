### Title
Dust-transfer griefing fills `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, DoSing deposits, borrows, and collateral additions for a victim - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Any unprivileged attacker can permanently occupy all `MAX_TOKENS_PER_USER` (30) slots of a victim's per-account token list by transferring dust amounts of every whitelisted `DepositToken` to the victim. Because `DepositToken._transfer` force-registers the recipient in `Pool.addToDepositTokensOfAccount`, and that function reverts once the combined debt+deposit token list reaches the cap, the victim can no longer deposit a new collateral type or be issued a new debt token type. For a victim whose position is fully drawn (`_issuableInUsd == 0`), the dust tokens are locked by `_revertIfLocked`/`unlockedBalanceOf`, so the victim cannot remove the attacker-inserted entries and cannot add a new collateral type to rescue a deteriorating position — a reachable denial of service analogous to the crafted-input crash/hang of CVE-2016-6711.

### Finding Description
`Pool.addToDepositTokensOfAccount` enforces the cap via `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`. [1](#0-0)  The same check gates `addToDebtTokensOfAccount`, used when a `DebtToken` mints to an account that previously held zero balance. [2](#0-1) 

Registration is involuntary: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance transitions from zero, with no opt-in or opt-out by the recipient. [3](#0-2)  `transfer` is a public entry point that only checks the *sender's* locked balance via `_revertIfLocked(_msgSender, amount_)`. [4](#0-3) 

Once the list is full:

- `DepositToken.deposit` → `_mint` → `addToDepositTokensOfAccount` reverts for any *new* collateral type the victim does not already hold (`_balanceBefore == 0 && amount_ > 0` path). [5](#0-4) 
- `DebtToken.issue`/mint for a debt token the victim does not already hold reverts identically, blocking new borrows.
- Removal requires the victim's own transfer/burn to zero, which calls `removeFromDepositTokensOfAccount` — but `transfer`/`withdraw` are gated by `_revertIfLocked`, and `unlockedBalanceOf` returns 0 when `_issuableInUsd == 0` (position at max borrow), so a fully-drawn victim cannot evict the dust entries while they need to add collateral most. [6](#0-5) 

Attack cost is 30 dust transfers (one wei–scale unit of each whitelisted `DepositToken`); a single deposit token type suffices to partially fill, and all whitelisted collaterals fill all slots. No privileged role, oracle manipulation, or trusted remote is required.

### Impact Explanation
Temporary freezing / denial of service of victim funds-management functions: the victim cannot onboard new collateral types or open new debt positions. In the worst case — a borrower at their issuance limit who needs to add a *different* collateral type to restore health — every cure path that touches a new token reverts, while `Pool.liquidate` remains callable against them, forcing liquidation losses that could have been avoided. The lock persists as long as the victim's position stays saturated (the dust cannot be removed while locked), making it a persistent rather than single-transaction DoS.

### Likelihood Explanation
- Fully permissionless: `DepositToken.transfer(to, 1)` is all that is required per slot.
- Deterministic: no race conditions, oracle dependence, or governance action needed.
- Moderate cost: bounded by the number of whitelisted deposit tokens (≤30) and gas for dust transfers; the attacker must acquire dust of each underlying first, which is trivial for liquid collaterals.
- Impact is partial (new token types only; existing collateral can still be topped up), which tempers severity to Medium, matching the CVE's medium/DoS profile.

### Recommendation
- Make per-account registration opt-in: only call `addToDepositTokensOfAccount` when `recipient_ == _msgSender()` (or when `to_ == onBehalfOf_` in `deposit`), and/or add a `Pool.removeFromDepositTokensOfAccount` escape path callable via a dedicated `unregister`-style function that bypasses `_revertIfLocked` for dust balances below a threshold.
- Alternatively, index the per-account lists by token at first *deposit* rather than first *received* balance, so unsolicited inbound transfers never occupy a slot.
- As defense-in-depth, allow `removeFromDepositTokensOfAccount` to succeed even when balance is locked if removing the entry cannot worsen solvency (e.g., token contributes zero to `_issuableLimitInUsd`).

### Proof of Concept
Hardhat (TypeScript) sketch, reproducible against the existing fixtures used in `test/DepositToken.test.ts`/`test/Pool.test.ts`:

```ts
// Setup: pool with N whitelisted deposit tokens (metDepositToken, msOTHER, ...),
// alice = victim with an open debt position at ~100% issuable (unlockedBalanceOf == 0),
// attacker holds dust of each underlying.

const tokens: DepositToken[] = [metDepositToken, /* ...all whitelisted DepositTokens... */];

// 1) Attacker dust-transfers 1 wei of each msdTOKEN to alice
for (const dt of tokens) {
  await dt.connect(attacker).deposit(1, attacker.address);
  await dt.connect(attacker).transfer(alice.address, 1);
}

// 2) Alice's token list is now at the cap
expect(await pool.getDepositTokensOfAccount(alice.address)).to.have.lengthOf(tokens.length);
// combined debtTokens + depositTokens >= MAX_TOKENS_PER_USER (30)

// 3) Alice cannot receive/deposit a NEW collateral type she didn't already hold
const newDepositToken = await deployNewWhitelistedDepositToken(); // governor whitelists one more type, or pick any she lacks
await newDepositToken.connect(alice).deposit(parseEther('10'), alice.address);
// => reverts with UserReachedMaxTokens (from addToDepositTokensOfAccount inside _mint)

// 4) Alice cannot evict the dust while fully drawn: unlockedBalanceOf(alice) == 0
expect(await tokens[0].unlockedBalanceOf(alice.address)).to.eq(0);
await expect(tokens[0].connect(alice).transfer(bob.address, 1))
  .to.be.revertedWithCustomError(tokens[0], 'NotEnoughFreeBalance');

// 5) Meanwhile liquidation of alice still works — the DoS blocks her only cure paths
//    involving new token types, while Pool.liquidate(...) executes normally.
```

Key invariants shown: (a) recipient cannot refuse slot occupation, (b) cap check at `Pool.sol:144` is hit on every subsequent first-touch of a new token, (c) `_revertIfLocked` + `unlockedBalanceOf == 0` at `DepositToken.sol:383-398` prevents cleanup while the position is saturated.

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

**File:** contracts/DepositToken.sol (L348-353)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
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

**File:** contracts/DepositToken.sol (L517-520)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
