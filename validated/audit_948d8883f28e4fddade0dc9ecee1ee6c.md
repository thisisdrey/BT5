### Title
Dust-transfer griefing fills `MAX_TOKENS_PER_USER` list and DoSes victim's deposits, borrows and liquidations - (File: contracts/Pool.sol)

### Summary
`Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once an account's combined `depositTokensOfAccount` + `debtTokensOfAccount` length reaches `MAX_TOKENS_PER_USER` (30). Because `DepositToken._transfer` adds the token to the *recipient's* list on any first-time receipt, an unprivileged attacker can dust-transfer 1 wei of each whitelisted msdToken to any victim, permanently occupying all 30 slots. After that, every code path that adds a new token to the victim's lists reverts, so the victim cannot deposit new collateral types, cannot receive msdTokens, and cannot open new debt positions. The analog to the CVE (unprivileged crash/DoS of a core service) is an unprivileged DoS of core pool operations.

### Finding Description
- `Pool.addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148, 216-220`).
- `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance transitions 0 → non-zero (`contracts/DepositToken.sol:517-520`). Public `transfer`/`transferFrom` only check the *sender's* unlocked balance (`DepositToken.sol:348-376`), so a dust amount is transferable by anyone holding the token.
- `DepositToken._mint` does the same on 0 → non-zero mints (`DepositToken.sol:486-488`), so `deposit(amount, onBehalfOf)` targeting a capped victim reverts (`DepositToken.sol:211-237`).
- `DebtToken` mint/issue paths register debt tokens through the same cap; a victim with 30 forced deposit-token entries cannot issue any new synthetic debt (`addToDebtTokensOfAccount`, `Pool.sol:204-208`).
- `Pool.liquidate` seizes collateral via `DepositToken.seize` → `_transfer` → `addToDepositTokensOfAccount(liquidator)`, which is unaffected for the victim, but the victim themselves cannot top up collateral or take any corrective action that requires receiving a new token.
- No modifier prevents it: `transfer` is not `nonReentrant`-gated against this, there is no opt-in/whitelist for receiving msdTokens, and the pool does not need to be paused — the revert fires on the normal deployed configuration whenever the victim already holds ≥1 token per slot filled.

### Impact Explanation
Temporary freezing of funds and blocked protocol operations for a targeted victim: all deposits of new collateral types (`deposit` reverts inside `_mint`), all inbound msdToken transfers, and all new borrows revert until the victim manually sweeps the dust (each sweep requires `unlockedBalanceOf > 0`, and a victim with maxed-out debt may have *zero* unlocked balance, in which case the dust cannot be moved and the freeze is effectively permanent for withdrawal-relevant operations). The cost to the attacker is ~30 dust transfers; the victim loses access to collateral management, which can also push a marginal position toward liquidation it cannot defend against.

### Likelihood Explanation
Fully permissionless: any EOA can acquire dust amounts of each msdToken (deposit 1 wei of each underlying or buy on market) and call `transfer`. No privileged role, oracle manipulation, or governance action is required. The attack must be renewed if the victim sweeps dust, but re-griefing is cheap and can be front-run on any sweep transaction.

### Recommendation
- Do not let *receiving* a token consume a slot: either drop the per-account cap, or track deposits via a mapping keyed by `(account, depositToken)` and iterate over the pool-level `depositTokens` set (bounded by `ReachedMaxDepositTokens`, a governor-controlled list) instead.
- Alternatively, only enforce the cap on the account's own *initiating* actions (deposit/issue) and let inbound dust transfers add entries without reverting, keeping `depositOf`/`debtOf` loops bounded by the governor-capped token list.

### Proof of Concept
Hardhat fork test:

```ts
// prerequisites: pool with >=30 whitelisted DepositTokens, victim holds no debt
const attacker = ...; const victim = ...;
for (const dt of await pool.getDepositTokens()) {
  const token = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('ERC20', await token.underlying());
  await underlying.connect(attacker).approve(token.address, 1);
  await token.connect(attacker).deposit(1, attacker.address);          // mint dust
  await token.connect(attacker).transfer(victim.address, 1);           // grief: fills victim's list
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// victim's own deposit of a NEW collateral type now reverts
const newToken = ...; // any deposit token victim doesn't hold
await expect(newToken.connect(victim).deposit(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// inbound transfers of a token victim doesn't hold also revert
await expect(otherToken.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Run against a mainnet/optimism fork at a deployed `Pool` (see `deployments/mainnet/Pool.json` and `deployments/optimism/Pool.json`, where `MAX_TOKENS_PER_USER` = 30 is in the ABI).