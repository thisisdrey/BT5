### Title
Dust `DepositToken` transfers fill a victim's `MAX_TOKENS_PER_USER` account list, DoS-ing deposits and debt issuance - (File: contracts/Pool.sol)

### Summary
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once an account holds `MAX_TOKENS_PER_USER = 30` entries across both lists. Because `DepositToken._transfer` pushes the recipient's token into this list on any nonzero incoming transfer, an unprivileged attacker can permissionlessly dust-transfer 30 distinct `DepositToken`s to a victim, permanently filling the victim's list. After that, every deposit of a new collateral type and every issuance of a new synthetic debt reverts for the victim. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
- `Pool` tracks per-account collateral and debt positions via `depositTokensOfAccount` / `debtTokensOfAccount` (`MappedEnumerableSet`), capped at `MAX_TOKENS_PER_USER = 30`; additions revert once the cap is reached. [4](#0-3) [1](#0-0) 
- `DepositToken._transfer` is reachable through the public, unauthenticated `transfer`/`transferFrom` (any holder can send to any address; `_revertIfLocked` only constrains the *sender's* own locked balance, not the recipient). When the recipient's prior balance is 0 and `amount_ > 0`, it calls `pool.addToDepositTokensOfAccount(recipient)`. [5](#0-4) [6](#0-5) 
- The attacker deposits a dust amount of each of the pool's `DepositToken`s (the base deployment ships many collaterals; debt tokens also count toward the shared 30-slot cap, so even fewer than 30 deposit tokens may suffice to fill the combined budget) and transfers 1 wei of each to the victim.
- Once the victim's combined `debtTokens + depositTokens` count reaches 30, `DepositToken._mint` (deposit path, which calls `addToDepositTokensOfAccount` on first receipt) reverts, and `DebtToken.issue`/`_mint` reverts via `addToDebtTokensOfAccount`. [7](#0-6) [8](#0-7) 
- The same mechanism can be fired at the exact moment a victim needs to top-up collateral: front-running the victim's `deposit` of a *new* collateral type with a dust transfer makes that deposit revert, so the victim cannot restore health with that collateral before liquidation.

### Impact Explanation
Temporary freezing of funds/functionality for a targeted account: the victim cannot deposit new collateral types and cannot issue new synthetic debt while the list is saturated. Combined with `deposit`'s revert this can prevent a victim from adding collateral to a deteriorating position at the critical moment, exposing them to liquidation they could otherwise have avoided. `withdraw`, `repay`, and liquidation are unaffected, so existing principal is not permanently locked, matching the "temporary freezing of funds" acceptance bar rather than theft or permanent loss.

### Likelihood Explanation
Fully unprivileged: requires only an EOA holding dust of each `DepositToken` (acquirable by depositing dust through the public `deposit` path or via gateways). No governor, guardian, keeper, oracle, or bridge trust assumptions are involved. Cost is bounded by the dust amounts plus gas. Mitigating factor: the victim can self-recover by transferring a dust token away (balance → 0 removes it from the list) or repaying a debt token to free a slot, so the DoS window is temporary and the attacker must re-fill slots, making this a griefing/leveraged-front-running issue rather than a permanent freeze.

### Recommendation
Do not auto-register tokens into `depositTokensOfAccount` on plain `transfer`/`transferFrom`/`seize` receipts; only register on `deposit`/`_mint` performed by the account itself (e.g., gate the `addToDepositTokensOfAccount` call in `_transfer` on the operation type, or let `seize` skip registration for the liquidator). Alternatively, raise/remove the cap by iterating lazily, or allow accounts to opt out of unsolicited registrations. At minimum, exclude unsolicited inbound transfers from counting toward `MAX_TOKENS_PER_USER` so third parties cannot consume a victim's slots.

### Proof of Concept
Hardhat sketch (test environment with `pool`, `depositTokens[]` from fixture):

```ts
// Attacker deposits dust and pushes it to the victim for each deposit token
for (const dt of depositTokens) {                      // up to MAX_TOKENS_PER_USER entries
  await underlying(dt).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST, attacker.address);
  await dt.connect(attacker).transfer(victim.address, 1); // registers victim in list
}
expect((await pool.getDepositTokensOfAccount(victim.address)).length)
  .to.eq(await pool.MAX_TOKENS_PER_USER());

// Victim can no longer deposit a new collateral type
await expect(
  newDepositToken.connect(victim).deposit(AMOUNT, victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Victim can no longer issue any new synthetic debt
await expect(
  debtToken.connect(victim).issue(AMOUNT, victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Front-run: attacker's dust transfer lands between victim's collateral
// deposit and a liquidation check, so the health-restoring deposit reverts.
```

Note: verification on a fork requires at least N live deposit tokens such that the victim's existing `debtTokens + depositTokens` count can be pushed to 30; on the base deployment with fewer listed deposit tokens, debt-token entries also count toward the same cap, which lowers the number of dust transfers needed.

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

**File:** contracts/Pool.sol (L204-208)
```text
    function addToDebtTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _debtToken = _msgSender();
        _revertIfSenderIsNotDebtToken(_debtToken);
        if (!debtTokensOfAccount.add(account_, _debtToken)) revert DebtTokenAlreadyExists();
    }
```

**File:** contracts/Pool.sol (L216-220)
```text
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
