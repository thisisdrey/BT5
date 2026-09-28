### Title
Dust deposits on behalf of a victim permanently fill their per-account token list, blocking new deposits/borrows and enabling forced liquidation - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The CVE describes a resource leak: repeated no-op inputs allocate memory that is never freed until the process dies. The Metronome analog is the per-account token lists in `Pool` (`depositTokensOfAccount` / `debtTokensOfAccount`). Each first-time receipt of a deposit token permanently appends an entry to the victim's list, and `onlyIfAdditionWillNotReachMaxTokens` reverts once the combined list reaches `MAX_TOKENS_PER_USER = 30`. An unprivileged attacker can dust-fill a victim's list via `DepositToken.deposit(amount, victim)` or `DepositToken.transfer(victim, dust)` across the pool's collateral types, after which the victim can no longer deposit any new collateral or mint a new debt token.

### Finding Description
`DepositToken._transfer` and `_mint` call `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance goes from 0 to non-zero (`DepositToken.sol:486-488`, `DepositToken.sol:517-520`). `Pool.addToDepositTokensOfAccount` enforces the cap:

```solidity
// contracts/Pool.sol:143-148
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

Entries are only removed when the holder's balance returns to zero (`DepositToken.sol:460-462`, `DepositToken.sol:522-524`), and there is no permission or consent check on receiving: `deposit(uint256 amount_, address onBehalfOf_)` mints directly to an arbitrary beneficiary (`DepositToken.sol:211-237`), and `transfer` to any address is allowed for unlocked balances (`DepositToken.sol:348-354`). Like the xmllint whitespace input, each dust deposit is a "no-op" economically but leaks one permanent storage slot in the victim's account list. After 30 combined entries, every subsequent add reverts with `UserReachedMaxTokens`, which DoS-es:
- `DepositToken.deposit` into any collateral the victim doesn't already hold (the victim cannot add collateral to improve an unhealthy position),
- `transfer`/`seize` of any msd token the victim doesn't hold (also breaks liquidator receives if the *liquidator* is griefed — see below),
- first borrow of a new synthetic (DebtToken mint → `addToDebtTokensOfAccount`).

### Impact Explanation
A victim with an open debt position approaching liquidation cannot deposit additional collateral of a new type to restore health — the tx reverts — so the position is liquidated and the attacker (or any liquidator) seizes collateral with the liquidation discount. Additionally, an attacker can grief known active liquidators/keeper EOAs so that `DepositToken.seize` reverts when paying out a token the liquidator doesn't already hold (`seize` → `_transfer` → `addToDepositTokensOfAccount` at `DepositToken.sol:343-345`), degrading liquidation liveness. The state growth is attacker-initiated and irreversible by the protocol (no admin function to prune a victim's list); the victim must spend gas transferring each dust token out fully to reclaim a slot.

### Likelihood Explanation
Fully unprivileged: the attacker only needs dust amounts of each whitelisted underlying (up to 30 deposit tokens per pool, `Pool.sol:703`). Cost is ~30 small deposits plus fees; no governor/keeper/oracle involvement. `whenNotPaused`/`nonReentrant` modifiers don't prevent it, and the cap check cannot distinguish legitimate receipts from griefing dust. Mitigation exists (victim can self-clean by transferring dust out), and the position-loss scenario requires the victim to be near liquidation, so impact is medium — matching the CVSS 6.2 DoS character of the source CVE.

### Recommendation
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert-free on the cap (silently skip enumeration, or return a bool) so transfers never revert, and track membership via `balanceOf > 0` checks in `debtOf`/`depositOf` instead of a capped list; or
- Apply the cap only to the *caller-initiated* action (`deposit` for self, `issue` for self) while allowing inbound transfers/`onBehalfOf` receipts to exceed it; or
- Require a minimum first-receipt amount (non-dust threshold) before adding to the per-account list; or
- Provide a `removeDustToken` escape hatch that lets an account drop list entries for tokens whose balance is below a dust threshold without a full transfer.

### Proof of Concept
Hardhat (fork) sketch:

```ts
// pool has >= 2 deposit tokens; attacker holds dust of each underlying
const victim = bob.address
for (const underlying of allPoolUnderlyingTokens) {
  const depositToken = await ethers.getContractAt('DepositToken',
    await pool.depositTokenOf(underlying))
  await underlyingToken.approve(depositToken.address, 1)
  // 1 wei deposit to victim; adds depositToken to victim's list
  await depositToken.deposit(1, victim)
  // reclaim attacker's own slot (optional) — victim's entries persist
}
// repeat until 30 entries (fill remaining via direct msd transfers)
// assert list full
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(30)

// victim tries to deposit a collateral type not yet held -> reverts
await expect(newDepositToken.connect(bob).deposit(1000, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// a new borrow for a synthetic not yet held also reverts inside DebtToken.issue
await expect(newDebtToken.connect(issuer).issue(bobAddr, 1)) // via pool.mint path
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

On a fork: deploy/attach to a live `Pool`, enumerate `getDepositTokens()` and `getSyntheticTokens()`, acquire dust of each underlying via AMM, call `deposit(1, victim)` for each until `getDepositTokensOfAccount(victim).length + getDebtTokensOfAccount(victim).length == 30`, then show victim's `deposit` on an unheld collateral and a liquidation-rescue top-up reverting with `UserReachedMaxTokens`.