### Title
Dust-transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, permanently reverting all subsequent deposits, mints and transfers-in of new token types - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The `MAX_TOKENS_PER_USER` cap in `Pool` is enforced inside `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`, which are invoked implicitly by `DepositToken._mint`/`_transfer` and `DebtToken` issuance. Because `DepositToken.transfer`/`transferFrom` are permissionless, an attacker can push dust balances of every listed deposit token to a victim, filling the victim's combined list to 30 entries. Every subsequent operation that would add a new entry for that account then reverts with `UserReachedMaxTokens` — a persistent, attacker-induced crash (revert) of the victim's protocol functionality, analogous to the Ghostscript NULL-deref DoS: a crafted input poisons state so a function that touches it always crashes.

### Finding Description
`Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (`30`) [1](#0-0) [2](#0-1) . The set mutation happens inside `addToDepositTokensOfAccount` [3](#0-2)  and `addToDebtTokensOfAccount` [4](#0-3) , callable only by registered tokens but triggered on behalf of arbitrary recipients.

`DepositToken._transfer` and `_mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient)` when the recipient's balance transitions from zero [5](#0-4) . `transfer`/`transferFrom` are unrestricted public functions, so anyone holding msdTOKEN can transfer dust to any victim. Similarly `DebtToken` issuance (`issue`/`mint`) calls `addToDebtTokensOfAccount` when the account's balance goes from 0 [6](#0-5) .

Attack: the attacker deposits dust collateral into each listed `DepositToken` (acquiring dust msdTOKEN balances cheaply) and transfers 1 wei of each to the victim via public `transfer`. Each transfer adds an entry to `depositTokensOfAccount[victim]`. Once the combined count reaches 30, every subsequent call that would register a new token for the victim reverts: `deposit`/`multiDeposit` of a new collateral type, `DebtToken.issue`/`mint`/`flashIssue`/`leverage` of a new synthetic, and any incoming transfer of a token type the victim doesn't already hold. Slots are only freed when the victim manually reduces a dust balance to zero, which costs the victim transactions per polluted slot (each `transfer` removes the token via `removeFromDepositTokensOfAccount` only when balance hits 0) [7](#0-6) .

### Impact Explanation
Temporary denial of service / freezing of protocol functionality for the victim: the victim cannot open new collateral positions or borrow new synthetics while the list is saturated. Any third-party or gateway flow that transfers a new token type to the victim (e.g., rewards paid in a new deposit token, SmartFarmingManager residual refunds of a token the victim doesn't hold) also reverts, which can brick multi-step flows built on top of `DepositToken` transfers. The victim retains withdrawals of existing positions, so existing principal is not permanently frozen — impact is a temporary, recoverable-but-costly freeze of deposit/borrow functionality plus reversion of inbound transfers of unlisted tokens.

### Likelihood Explanation
Likelihood is moderate-to-high mechanically: the attack only requires the attacker to hold dust balances of each listed deposit token and pay gas for ≤30 transfers; there is no access control on `transfer`/`transferFrom` and no opt-out on the recipient side. Constraints: the number of deposit tokens per pool is itself capped at 30 [8](#0-7) , so filling the list requires the pool to list many collateral types; on pools with few collateral types the attacker cannot reach the cap via deposit tokens alone and cannot force debt-token entries (issuance requires the victim's own mint, though `issue`/`mint` with `onBehalfOf_ = victim` is permissionless — `DebtToken.issue` mints debt to `onBehalfOf_`, but it requires collateral backing on `debtPositionOf(onBehalfOf_)`, limiting abuse to victims who already have issuable headroom). Where it works, the attack is cheap, repeatable against any account, and requires no privileged role, matching the unprivileged-attacker model.

### Recommendation
Decouple the cap from unrequested inbound flows:
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` fail-soft (return bool, skip adding on saturation) so dust transfers can't make victim calls revert, or
- Only count entries above a minimum balance threshold toward `MAX_TOKENS_PER_USER`, or
- Allow victims a `removeFromMyTokens(address)` escape hatch that clears dust entries without requiring a full token transfer, and let `deposit`/`issue` auto-eject zero-or-dust entries when at cap.

### Proof of Concept
Hardhat fork sketch (pool with ≥1 listed deposit token per iteration; repeat across all listed deposit tokens until the victim's `debtTokensOfAccount.length + depositTokensOfAccount.length == 30`):

```ts
// attacker deposits dust into each listed DepositToken to mint dust msdTOKEN
for (const dt of depositTokens) {
  await underlying.mint(attacker, DUST);
  await underlying.connect(attacker).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST, attacker.address);
  // grief: push the dust position token to the victim
  await dt.connect(attacker).transfer(victim.address, 1);
}

// victim's list is now saturated
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// victim can no longer deposit into a new collateral type
await expect(newDepositToken.connect(victim).deposit(1e18, victim.address))
  .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// victim can no longer issue a new synthetic debt
await expect(newDebtToken.connect(victim).issue(1e18, victim.address))
  .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// inbound transfer of any token the victim doesn't hold also reverts
await expect(otherDepositToken.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");
```

Notes on uncertainty: I verified the modifier and add/remove call sites in `Pool.sol`/`DepositToken.sol`/`DebtToken.sol`. Whether the deposit side alone reaches 30 depends on the deployed number of `depositTokens` (also capped at 30); on pools with few listed collaterals the attack needs the debt-token side as well, which requires the victim to have `issuableInUsd > 0` (attacker calling `issue(x, victim)` against the victim's collateral). Reclaiming slots requires the victim to spend gas transferring each dust balance to zero. These constraints make the severity closer to "temporary freezing of functionality" than fund loss, but the revert-poisoning mechanism is a direct analog of the crafted-input crash class.

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

**File:** contracts/Pool.sol (L703-705)
```text
        if (depositTokens.length() >= MAX_TOKENS_PER_USER) revert ReachedMaxDepositTokens();

        if (!depositTokens.add(depositToken_)) revert DepositTokenAlreadyExists();
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

**File:** contracts/DebtToken.sol (L539-540)
```text
        // Remove this token from the debt tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf(account_) == 0) {
```
