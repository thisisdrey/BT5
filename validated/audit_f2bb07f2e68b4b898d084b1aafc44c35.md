### Title
Unauthenticated dust-transfer griefing fills `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, permanently blocking a victim's deposits, borrows, and inbound transfers - (File: contracts/DepositToken.sol)

### Summary
Analogous to CVE-2016-7797 (an unauthenticated remote peer causes denial of service / node disconnection), any unprivileged account can force-add entries to a victim's per-account deposit-token list in `Pool` simply by transferring dust amounts of `DepositToken` (msdTOKEN) receipts to them. Once `debtTokensOfAccount + depositTokensOfAccount` reaches `MAX_TOKENS_PER_USER = 30`, every subsequent first-time deposit, borrow (`issue` of a new `DebtToken`), and inbound `transfer`/`seize` to that account reverts with `UserReachedMaxTokens`, disconnecting the victim from the protocol's core entry points.

### Finding Description
`DepositToken._transfer` adds the token to the recipient's pool tracking list whenever the recipient's prior balance is zero, with no opt-in check on the recipient: [1](#0-0) 

That call lands in `Pool.addToDepositTokensOfAccount`, gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once the account holds 30 combined debt/deposit token entries: [2](#0-1) [3](#0-2) 

The same `add` path is triggered from `DepositToken._mint` on `deposit` and from `DebtToken` issuance (`addToDebtTokensOfAccount`), so once filled, the victim cannot: deposit any collateral type they don't already hold, issue any new synth debt, or receive any msdTOKEN transfer — including `seize` proceeds routed to them during `Pool.liquidate` if they are the `to_` party.

### Impact Explanation
Liveness/availability break equivalent to "node disconnection": the victim is cut off from opening or expanding positions. Practically, a victim whose position is deteriorating cannot deposit a new collateral type to restore health and cannot be topped up by a third party sending msdTOKEN — every such transaction reverts until the victim manually empties a dusted token balance to zero (which itself costs transactions and may be re-griefed). This is temporary-to-persistent freezing of the account's protocol functionality caused by a purely unauthenticated action.

### Likelihood Explanation
The attack requires no privilege: the attacker deposits dust underlying into each `DepositToken` of a pool (or obtains them on the market) and calls `transfer(victim, 1)` for each. Cost is bounded by the number of registered deposit/debt tokens in the pool and gas. No guardian, governor, oracle, or bridge role is involved, and no pause/shutdown flag, reentrancy guard, or health check prevents it — `transfer` only checks the sender's unlocked balance via `_revertIfLocked`.

### Recommendation
Make list membership opt-in for transfers: only call `pool.addToDepositTokensOfAccount` on `_mint`/deposit paths, or add a `Pool`-side allowlist such that `addToDepositTokensOfAccount` invoked from `_transfer` does not revert the transfer but skips tracking (track only on deposit/withdraw/seize accounting). Alternatively, permit the recipient to remove list entries permissionlessly even with nonzero balance, so victims can clear dusted slots.

### Proof of Concept
Hardhat fork sketch:

```ts
// setup: pool with N registered DepositTokens dt_0..dt_k
const attacker = signs[0], victim = signs[1];

// 1. Attacker acquires dust of every deposit token
for (const dt of depositTokens) {
  await underlying(dt).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST, attacker.address);
}

// 2. Fill victim's account list via unsolicited dust transfers
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victim.address, 1n);
}
// victim's depositTokensOfAccount length == number of pool deposit tokens
// combined with any debt tokens, reaches MAX_TOKENS_PER_USER (30)

// 3. Victim can no longer deposit a collateral type they don't hold
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// 4. Nor issue a new debt token / receive transfers
await expect(
  debtTokenNew.connect(victim).issue(amount, victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");
```

Note: full reachability depends on the deployed pool having enough registered deposit/debt tokens to reach the cap of 30; if a pool has few collateral types, the attacker fills all available slots but cannot reach the cap, in which case severity degrades to blocking only new-token onboarding rather than a hard DoS. This could not be fully verified against live deployment counts from the indexed code alone.

### Citations

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
