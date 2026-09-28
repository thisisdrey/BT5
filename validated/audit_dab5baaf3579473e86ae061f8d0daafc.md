The DoS bug class maps to a reachable griefing surface in `Pool`'s per-account token lists. Checking the debt-token side to confirm the block path.

The mechanism: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` for any first-time receipt (`contracts/DepositToken.sol:518-520`), and `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the combined deposit+debt token count hits `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:143-148, 216-220`). The same cap guards `addToDebtTokensOfAccount` (`contracts/Pool.sol:204-208`), which `DebtToken` mints trigger on first borrow. An attacker with no debt has a fully unlocked balance (`unlockedBalanceOf` returns `balanceOf` when `_debtInUsd == 0`, `DepositToken.sol:386-390`), so they can dust-transfer 1 wei of every listed `DepositToken` to a victim. Once 30 slots are filled, the victim cannot mint any new `DebtToken` (first `issue` always calls `addToDebtTokensOfAccount`) and cannot deposit any collateral type they don't already hold — reverting every rescue path when the position needs a new collateral type. This is a persistent, attacker-induced account liveness failure, the analog of the CVE's availability impact, reachable by any EOA with no privileged role and already demonstrated by the `UserReachedMaxTokens` tests (`test/Pool.test.ts:1386-1416`).

### Title
Dust-transfer griefing fills `depositTokensOfAccount` and permanently blocks victim from borrowing or depositing new collateral types - (File: contracts/Pool.sol)

### Summary
`Pool` tracks per-account deposit/debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30`. Any unprivileged user can `transfer` dust amounts of each whitelisted `DepositToken` to a victim, and each first-time receipt appends an entry via `addToDepositTokensOfAccount`. Once the victim's combined list reaches 30, `onlyIfAdditionWillNotReachMaxTokens` reverts every subsequent addition — meaning the victim can never issue a new debt token nor deposit a collateral type they don't already hold, until they manually clear the dust entries one by one.

### Finding Description
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance (`DepositToken.sol:348-376`); a debt-free attacker is fully unlocked and may send arbitrarily small amounts (`amount_ > 0`).
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 (`DepositToken.sol:518-520`). The recipient cannot refuse the transfer.
- `Pool.addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` enforce `MAX_TOKENS_PER_USER` on the *recipient's* list and revert with `UserReachedMaxTokens` (`Pool.sol:143-148`, `204-220`).
- `DebtToken` issuance calls `addToDebtTokensOfAccount` on first mint, so a victim with a saturated list cannot borrow *any* synthetic asset (`issue`, `leverage` all revert).
- `DepositToken._mint` calls `addToDepositTokensOfAccount` for first-time deposits (`DepositToken.sol:486-488`), so the victim cannot add a new collateral type — e.g., to rescue a deteriorating position — while the dust remains.
- Removing a dust entry requires the victim to transfer/withdraw their entire dust balance of that token; with 30 poisoned entries this requires up to 30 transactions, and the attacker can re-dust cheaply at any time.
- The deployed configuration offers no mitigation: `addToDepositTokensOfAccount` is callable by any registered deposit token via `_msgSender()` (SynthContext), and neither pause flags nor the reentrancy guard prevent it.

### Impact Explanation
Availability/liveness break for a targeted account: the victim is denied all borrowing (every debt-token mint reverts) and all new-collateral deposits. For a leveraged victim approaching the liquidation threshold, the inability to deposit additional collateral of a new type can force an otherwise avoidable liquidation, converting the DoS into loss of funds. The attack is permissionless, cheap (30 dust transfers of free `msdToken` shares), and repeatable after cleanup.

### Likelihood Explanation
Only requires: the pool has multiple registered `DepositToken`s (deployed pools do), the attacker holds a small unlocked balance of each (easily obtained by depositing or buying dust), and the victim's list has capacity. No privileged role, oracle manipulation, or governance action is needed. Cost is linear in slots and fully under attacker control; mitigated only by the victim spending gas to clear entries.

### Recommendation
Do not let inbound transfers consume the recipient's capped list, or make the cap non-blocking:
- In `Pool.addToDepositTokensOfAccount`, skip (rather than revert) when the account is at the cap for unsolicited transfer-driven additions, or exclude dust below a minimum threshold.
- Alternatively, track collateral for health checks via a separate mechanism that doesn't gate `issue`/`deposit` on unsolicited inbound transfers, or split the caps so dust deposit tokens cannot block debt-token issuance.

### Proof of Concept
Hardhat (matches existing `UserReachedMaxTokens` tests in `test/Pool.test.ts`):

```ts
// victim starts with empty lists
for (const dt of allDepositTokens /* >= 30 registered deposit tokens or combined with debt tokens */) {
  await dt.connect(attacker).transfer(victim.address, 1); // dust, attacker unlocked
}
// victim now has 30 entries; every new addition reverts:
await expect(debtToken.connect(victim).issue(msUSD.address, amount))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
await expect(newDepositToken.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** contracts/DepositToken.sol (L348-354)
```text
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

**File:** contracts/DepositToken.sol (L486-488)
```text
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
