### Title
Dust transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, DoSing deposits of new collateral types - ([File: contracts/Pool.sol])

### Summary
The CVE-2019-18359 bug class is a crash/denial-of-service triggered by processing attacker-influenced input. In Metronome the closest reachable analog is a revert-based DoS: `Pool.addToDepositTokensOfAccount` reverts once an account's combined debt + deposit token list reaches `MAX_TOKENS_PER_USER` (30). Since `DepositToken` balances are freely transferable, an attacker can push dust amounts of deposit tokens to a victim, occupying the victim's list slots and causing every subsequent first-time deposit or deposit-token receipt for a new collateral type to revert.

### Finding Description
`Pool` tracks per-account collateral/debt in `MappedEnumerableSet` lists guarded by `onlyIfAdditionWillNotReachMaxTokens`:

- `MAX_TOKENS_PER_USER = 30` and the modifier reverts with `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` [1](#0-0) 
- `addToDepositTokensOfAccount` is only callable by a registered `DepositToken`, i.e. via `_transfer`/`_mint` inside the token [2](#0-1) 
- `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 — no recipient opt-in [3](#0-2) 
- The same happens on mint: `pool.addToDepositTokensOfAccount(account_)` inside `_mint` [4](#0-3) 

Attack path (fully unprivileged):

1. Attacker deposits into every `DepositToken` in the pool (or acquires them), obtaining transferable msdTOKEN balances.
2. Attacker calls `depositToken.transfer(victim, 1)` for each deposit token the victim doesn't already hold. Each call adds an entry to the victim's `depositTokensOfAccount` list.
3. Once the victim's combined list length reaches 30, any attempt by the victim to deposit a collateral type they don't already hold reverts in `_mint → addToDepositTokensOfAccount → UserReachedMaxTokens`, and any deposit-token transfer to them also reverts.

No privileged role is needed; `onlyIfAdditionWillNotReachMaxTokens`, reentrancy guards and pause flags do not stop the dust transfers themselves.

### Impact Explanation
The victim is DoS'd from onboarding new collateral types. Concretely: a user whose position is deteriorating and who needs to deposit a *different* collateral to restore health cannot — the deposit reverts — so their position is liquidated where it would otherwise have been saved (temporary freezing of the deposit function / forced liquidation). It also permanently blocks receipt of new deposit tokens (transfers to the victim revert) until the victim manually transfers dust out to shrink the list — during which the attacker can re-fill slots by front-running. This is a crash-class DoS analog: attacker-supplied input drives the victim's state transitions into an always-reverting path.

Feasibility caveat: the attack requires the pool to have enough distinct `DepositToken`s (plus victim's existing debt-token entries) to reach the 30-entry cap; on deployments with few collateral types the attacker can only partially fill the list, reducing but not eliminating the griefing surface.

### Likelihood Explanation
Cost is bounded: one dust transfer per deposit token, paid from the attacker's own balance. The victim can evict entries by transferring dust balances away (which calls `removeFromDepositTokensOfAccount` when the balance hits zero), but eviction is front-runnable and requires the victim to notice and act — impractical inside a liquidation emergency. Medium likelihood on pools with many deposit tokens; low on pools with few.

### Recommendation
- Do not let arbitrary incoming transfers populate `depositTokensOfAccount`; only add entries through the `deposit`/`mint` path where the account (or an authorized operator) opted in, or make `transfer` require recipient list membership be pre-established.
- Alternatively, remove the cap's revert coupling: allow receipts beyond the cap (track positions without enumerable-set membership for accounting-critical paths), since `depositOf`/`debtOf` loops are already bounded by the number of registered tokens.

### Proof of Concept
Hardhat fork sketch:

```ts
// Pool with N registered DepositTokens; victim has existing entries totalling >= 30 - N
for (const dt of depositTokens) {
  // attacker holds some balance of dt
  if ((await dt.balanceOf(victim)).eq(0)) {
    await dt.connect(attacker).transfer(victim.address, 1);
  }
}
// victim's combined list is now at MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(victim.address)).length.gte(...);

// victim tries to deposit a collateral type not already in their list
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// collateral: transfers of any new deposit token to victim also revert
await expect(
  dt.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Key invariant broken: liveness of the deposit path for a targeted account, reachable from permissionless `DepositToken.transfer`.

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

**File:** contracts/Pool.sol (L216-220)
```text
    function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
        address _depositToken = _msgSender();
        _revertIfSenderIsNotDepositToken(_depositToken);
        if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
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
