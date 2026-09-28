### Title
Unsolicited dust transfers fill a victim's `MAX_TOKENS_PER_USER` slot list, blocking new collateral deposits and forcing liquidation - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to the Shardus `joinarchiver` dedup flaw (registering one entity many times under different keys until a hard cap is exhausted), any EOA can "register" a victim account into many per-account token entries without the victim's consent. `DepositToken._transfer` calls `Pool.addToDepositTokensOfAccount` whenever the recipient's balance goes 0 → >0 (contracts/DepositToken.sol:517-520). The per-account lists `depositTokensOfAccount`/`debtTokensOfAccount` are capped at `MAX_TOKENS_PER_USER = 30` (contracts/Pool.sol:79), enforced by `onlyIfAdditionWillNotReachMaxTokens` (contracts/Pool.sol:143-148). There is no dedup by economic identity (underlying asset) and no opt-in: each distinct whitelisted deposit token occupies a slot, and an attacker needs only dust of each token.

### Finding Description
Attack path:
1. Attacker deposits small amounts of every whitelisted collateral (or buys dust on market and calls `Pool.deposit`) to obtain 1 wei of each `DepositToken`.
2. Attacker calls `DepositToken.transfer(victim, 1)` for each token. Each transfer pushes the token into `depositTokensOfAccount[victim]` via `pool.addToDepositTokensOfAccount(victim)` (Pool.sol:216-220, which reverts only on `DepositTokenAlreadyExists` — set dedup is by contract address, not underlying, but here dedup doesn't even matter since each token is distinct).
3. Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) >= 30`, any subsequent action that would add a *new* token to the victim's lists reverts with `UserReachedMaxTokens`:
   - `deposit()` into a collateral type the victim doesn't already hold.
   - `issue()`/`mint` of a synthetic whose `DebtToken` the victim doesn't already hold (`DebtToken._issue` → `addToDebtTokensOfAccount`, revert at Pool.sol:204-208).

The victim cannot prevent the registrations (transfers are permissionless). Clearing a slot requires the victim to transfer the entire dust balance of that token out (balance → 0 triggers `removeFromDepositTokensOfAccount`, DepositToken.sol:523-525), and the attacker can re-dust in the next block to re-fill the slot.

### Impact Explanation
The broken invariant is account-list liveness: a user's ability to open/maintain positions is silently capped by third-party pushes. Concretely:
- A victim whose position approaches liquidation and who needs to deposit a *new* collateral type (e.g., their remaining funds are in a token they haven't deposited before) is blocked; `deposit` reverts and the position is liquidated, costing the victim the liquidation penalty (protocol fee + liquidator discount via `Pool.liquidate`/`DepositToken.seize`).
- A victim who wants to open debt in a new synthetic is fully blocked (temporary freezing of borrowing capability) while the attacker keeps slots occupied.
This is a temporary freezing / forced-liquidation impact, not theft of principal.

### Likelihood Explanation
Requires the pool to have enough whitelisted tokens for the attacker to actually reach 30 combined entries on the victim (DebtTokens are non-transferable — `TransferNotSupported` in DebtToken.sol — so the attacker can only push deposit-token entries and relies on the victim's existing debt entries to help reach the cap). `addDepositToken` itself caps the pool at 30 deposit tokens (Pool.sol:703), so feasibility depends on deployment: pools with ~30 collaterals make this cheap; pools with few collaterals make it unreachable. Cost to the attacker is dust in each collateral plus gas; re-dusting after each victim cleanup is cheap. It is fully permissionless — no governor/keeper/oracle involvement.

### Recommendation
- Make additions to `depositTokensOfAccount`/`debtTokensOfAccount` opt-in: only add on user-initiated actions (`deposit`, `issue`), not on incoming `transfer`/`transferFrom`; or
- Have `transfer` revert/skip the registration when the recipient is at the cap (let the balance increase without registering, since the set is used for position accounting), or allow `addToDepositTokensOfAccount` to succeed silently (return instead of `UserReachedMaxTokens` revert) when called from a transfer context while still reverting on deposit/mint.

### Proof of Concept
Hardhat sketch (against deployed-pool fork or local fixture):

```ts
// assume `pool` lists >= 30 deposit tokens; attacker holds dust of each
const victim = bob.address
for (const dt of depositTokens /* all whitelisted DepositToken contracts */) {
  await dt.connect(attacker).transfer(victim, 1) // fills slot
}
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(30)

// victim deposits a token not yet in list -> reverts
await expect(
  newDepositToken.connect(victimSigner).deposit(amount)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim issues a synth whose DebtToken isn't in debtTokensOfAccount -> reverts
await expect(
  newDebtToken.connect(victimSigner).issue(synthAmt)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim's undercollateralized position is liquidated while deposit path is blocked
await pool.connect(liquidator).liquidate(synthetic, victim, amountIn, amountOutMin)
```

Note: where the pool has fewer than ~30 whitelisted tokens total, the cap cannot be reached by dust alone and the attack degrades to blocking only the last few slots of users already holding many positions — verify token count on the target deployment.