### Title
Unprivileged attacker can fill a victim's `depositTokensOfAccount` list via dust `DepositToken.transfer`, bricking deposits, mints and repay-freezing of new token types - ([File: contracts/Pool.sol](contracts/Pool.sol), [File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` on the combined size of an account's `debtTokensOfAccount` + `depositTokensOfAccount` sets. Entries are added inside `DepositToken._transfer` whenever a recipient's balance goes from 0 to >0 — including on plain ERC20 `transfer`/`transferFrom`, which any holder of `msdTOKEN` shares can call with arbitrary `to_`. An attacker can therefore push 1 wei of every whitelisted deposit token to a victim, filling the victim's list to the cap. After that, every code path that would add a *new* token to the victim's account reverts with `UserReachedMaxTokens`, denying the victim service (depositing new collateral types, issuing a new synth debt, receiving seized/liquidation collateral in a new token).

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever `_recipientBalanceBefore == 0 && amount_ > 0` (contracts/DepositToken.sol:517-520). `transfer`/`transferFrom` only check the sender's *unlocked* balance (`_revertIfLocked`, lines 350, 362) — there is no opt-in or cap check for the recipient.
- `Pool.addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount + depositTokensOfAccount >= 30` (contracts/Pool.sol:143-148).
- The same check gates `_mint` inside `DepositToken.deposit` (line 486-488) and `DebtToken._mint` (contracts/DebtToken.sol:598-600), which is reached by `issue`, `mint` (SmartFarmingManager leverage), and liquidation `seize` when the recipient has no prior balance of that token.
- Attack: attacker deposits dust into each of the pool's deposit tokens (or acquires msd shares), then calls `msdToken_i.transfer(victim, 1)` for each token until the victim's combined list hits 30. Every subsequent `deposit(amount, victim)` / `issue` / `leverage` for a token the victim does not already hold reverts.

### Impact Explanation
- A victim whose position is approaching liquidation and whose only viable rescue collateral is a token they do not yet hold cannot deposit it — `deposit` reverts inside `_mint` → `addToDepositTokensOfAccount`. The position becomes liquidatable and the liquidator profit is guaranteed. This is a liveness break of the deposit/mint path (analogous to the CVE's hang/crash + unauthorized modification of victim state).
- The freeze is temporary/recoverable (victim can transfer dust shares out to free slots, at gas cost and requiring the victim to notice), matching the "temporary freezing of funds" acceptance bar.
- No privileged role, oracle manipulation, or malicious endpoint is required: the attacker only uses public `DepositToken.transfer` and standard deposit/mint entry points.

### Likelihood Explanation
- Cost is bounded: the attacker must hold >0 of up to ~30 deposit tokens. On mainnet deployments the number of listed deposit tokens is smaller than 30, so the attack only fully bricks accounts if the victim already holds some tokens; with debt tokens counted too, a victim holding any existing positions is closer to the cap. Feasibility therefore depends on the number of listed tokens (N deposit tokens listed → attacker needs `30 - N_debt_held - N_dep_held` pushes). Where a pool lists ≥30 tokens, a fresh victim can be fully bricked for the cost of dust deposits.
- The victim's escape (transferring dust out token-by-token) requires off-chain awareness and multiple transactions, during which liquidations proceed.

### Recommendation
- Make recipient opt-in for list membership: e.g., skip `addToDepositTokensOfAccount` on unsolicited `transfer` (track deposits vs. transfers separately), or whitelist pushed adds so only `deposit`/`seize`/`_mint` add entries.
- Alternatively, raise `MAX_TOKENS_PER_USER`, auto-evict dust entries, or let `addToDepositTokensOfAccount` silently skip instead of reverting when the cap is reached (the list is only used for collateral accounting in `depositOf`, so skipping a 1-wei entry is economically safe — verify this does not undercount real collateral).

### Proof of Concept
Hardhat sketch against deployed-style fixture:

```ts
// victim holds 0 tokens; pool lists >= 30 deposit tokens (or victim already holds some)
for (const msd of depositTokens /* first 30 listed */) {
  // attacker: deposit dust then push
  await underlying.approve(msd.address, 1);
  await msd.connect(attacker).deposit(1, attacker.address);       // or transfer existing shares
  await msd.connect(attacker).transfer(victim.address, 1);        // adds msd to victim's list
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// victim tries to deposit a new collateral type (token #31)
await newUnderlying.approve(newMsd.address, amount);
await expect(newMsd.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim tries to mint a synth they never held -> DebtToken._mint -> addToDebtTokensOfAccount reverts
await expect(newDebtToken.connect(victim).issue(mintAmt, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// meanwhile victim's existing position drifts unhealthy -> liquidate succeeds, victim cannot add collateral
```

Run against a mainnet fork deployment where the listed deposit-token count ≥ 30 minus victim's existing entries; otherwise partially brick the remaining slots.