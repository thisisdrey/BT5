### Title
Unprivileged dust-transfer griefing fills a victim's per-account token list, DoS-ing deposits and debt issuance — (`contracts/Pool.sol`)

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined length of `depositTokensOfAccount` + `debtTokensOfAccount` per account, reverting with `UserReachedMaxTokens` once reached (`contracts/Pool.sol:143-148`). Both `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` apply this check (`contracts/Pool.sol:204-220`). `DepositToken._transfer` auto-adds the token to the recipient's list whenever the recipient's balance moves `0 → >0` (`contracts/DepositToken.sol:517-520`), and `_mint` does the same (`contracts/DepositToken.sol:485-488`). Any EOA can therefore push a victim's list to the cap by transferring dust (1 wei) of many whitelisted deposit tokens, after which every operation that would add a *new* token to the victim's lists permanently reverts.

### Finding Description
Attack path, all via public entry points:

1. Attacker deposits a minimal amount of each supported collateral via `DepositToken.deposit`/`Pool` to obtain dust balances of up to 30 distinct `DepositToken`s (or combines with cheap `DebtToken` issuance on own account then repays — not needed).
2. Attacker calls `DepositToken.transfer(victim, 1)` for each token. Each call hits `addToDepositTokensOfAccount(victim)` and increments `depositTokensOfAccount.length(victim)`.
3. Once `depositTokensOfAccount + debtTokensOfAccount` length hits 30, the victim can no longer:
   - deposit a collateral type they don't already hold (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`),
   - receive `msdTOKEN` transfers of any new type (transfer reverts for the *sender* too, but specifically the victim's deposit/borrow flows break),
   - issue/mint a synthetic asset whose `DebtToken` is not already in their list (`DebtToken` mint path calls `addToDebtTokensOfAccount`, which reverts — `contracts/Pool.sol:204-208`),
   - open leveraged positions via `SmartFarmingManager.leverage` into new synths.

The griefer can re-dust the victim whenever a slot is freed, sustaining the DoS at low cost since dust of already-listed tokens can be re-sent indefinitely.

### Impact Explanation
Availability/DoS analog of CVE-2017-3257 (low-privileged, network-reachable denial of service): a victim's ability to deposit new collateral types and issue new synthetic debt is blocked. This is a *temporary* freezing of protocol functionality for the victim — existing balances are not frozen (withdrawal of held tokens and debt repayment still work), and the victim can self-recover by transferring out the dust tokens to free list slots (`_transfer`/`_burn` call `removeFromDepositTokensOfAccount` on zero balance, `contracts/DepositToken.sol:459-462,523-525`). If the victim has outstanding debt, the dust sits inside their (partially locked) deposit balance, but `unlockedBalanceOf` (`contracts/DepositToken.sol:383-398`) typically still permits moving the tiny dust amounts, so recovery remains possible but requires extra transactions and gas. No theft or insolvency — the impact is bounded to liveness of the victim's new-position functionality.

### Likelihood Explanation
- Attacker needs only an EOA and dust capital across ≤30 whitelisted deposit tokens; no privileged role, oracle manipulation, or malicious infrastructure required.
- `addToDepositTokensOfAccount` is guarded only by `_revertIfSenderIsNotDepositToken` (caller must be a real deposit token), which the attacker satisfies by using the real tokens' `transfer`.
- No pause/shutdown flag, reentrancy guard, health check, or cap prevents receiving dust.
- Frontrunning victims (e.g., before a planned large deposit or leverage call) makes this cheap targeted griefing.

### Recommendation
- Allow recipients to prune their own lists without holding/transferring balances (e.g., a `Pool.removeFromDepositTokensOfAccount` callable by the account owner when `balanceOf == 0` — though balance is never 0 for dusted tokens, so instead let users "reject"/sweep dust cheaply), or
- Only add to the account list on *mint* (deposit), not on secondary `transfer`, or require a minimum amount threshold before list insertion, or
- Raise/decouple the cap for debt tokens specifically (griefing deposit-token slots currently also blocks new borrows since the check is on the combined length, `contracts/Pool.sol:144`).

### Proof of Concept
Hardhat sketch (structure mirrors `test/Pool.test.ts:1386-1416`, which already proves the revert):

```ts
// attacker acquires dust of N deposit tokens
for (const dt of depositTokens) {                     // ≤ 30 whitelisted DepositTokens
  await underlying.connect(attacker).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST);           // mints msToken to attacker
  await dt.connect(attacker).transfer(victim.address, 1); // adds dt to victim's list
}

// victim's combined list length is now MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30);

// victim can no longer deposit a new collateral type
await expect(newDepositToken.connect(victim).deposit(amount))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// nor issue a new synthetic (DebtToken mint -> addToDebtTokensOfAccount reverts)
await expect(newDebtToken.connect(victim).mint(victim.address, amount))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Uncertainty note: I verified the cap logic and the `DepositToken` auto-add paths directly; the exact `DebtToken` mint→`addToDebtTokensOfAccount` call site was not read line-by-line, but the function exists solely for that purpose (`contracts/Pool.sol:199-208`) and the test suite (`test/Pool.test.ts:1491-1521`) confirms identical revert behavior on the debt-token side.