### Title
Attacker can dust-fill a victim's per-account token list to `MAX_TOKENS_PER_USER`, blocking the victim from depositing new collateral types or issuing new debt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome tracks each account's collateral and debt positions in two enumerable per-account sets (`depositTokensOfAccount`, `debtTokensOfAccount`) capped at `MAX_TOKENS_PER_USER = 30`. Entries are added permissionlessly whenever a `DepositToken` balance changes from `0` — including via plain `transfer()`. An unprivileged attacker can deposit dust into every supported collateral and then transfer 1 wei of each `DepositToken` to a victim, filling the victim's combined token list to the cap. Once full, any action that would add a new token type to the victim's list reverts with `UserReachedMaxTokens`, so the victim cannot deposit a new collateral type, cannot be onboarded to a new debt token via `issue()`, and cannot receive `DepositToken` transfers — mirroring the "invalid entry in a shared list breaks core functionality" class.

### Finding Description
In `Pool.sol`, `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` enforce `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER → UserReachedMaxTokens` ([Pool.sol L143-L148, L204-L220](contracts/Pool.sol)). These functions are callable by any registered `DepositToken`/`DebtToken` contract.

`DepositToken._transfer` adds the token to the **recipient's** list whenever their prior balance was `0`, with no opt-in and no minimum amount ([DepositToken.sol L517-L520](contracts/DepositToken.sol)). The only prerequisite for the sender is an unlocked balance (`_revertIfLocked`, [L348-353](contracts/DepositToken.sol)), which the attacker trivially satisfies by depositing dust themselves. Likewise `DepositToken._mint` (used by `deposit(amount_, onBehalfOf_)`) adds to `onBehalfOf_`'s list ([L486-488](contracts/DepositToken.sol)), and `DebtToken._mint` adds to the borrower's list ([DebtToken.sol L597-600](contracts/DebtToken.sol)), both propagating the `UserReachedMaxTokens` revert.

Attack path (all public entry points):
1. For each of up to 30 supported deposit tokens, attacker calls `depositToken.deposit(dust, attacker)`.
2. Attacker calls `depositToken.transfer(victim, 1)` for each token — each adds an entry to `depositTokensOfAccount[victim]`.
3. `depositTokensOfAccount[victim].length + debtTokensOfAccount[victim].length == 30`.
4. Any subsequent `deposit(newToken, victim)` (including collateral top-ups during a liquidation window), `issue()` of a debt token the victim doesn't already hold, `DepositToken.transfer/transferFrom/seize` to the victim of a token type not already in the list, all revert with `UserReachedMaxTokens`.

### Impact Explanation
Temporary freezing of the deposit pathway and forced-liquidation exposure. A victim whose combined list is full cannot add any *new* collateral type to their position. If the victim's position is near liquidation and the needed collateral asset is not already in their list, the attacker can front-run top-up `deposit()` transactions to keep the position liquidatable, converting the DoS into loss of funds via `Pool.liquidate`. The victim can recover by withdrawing any single dusted token to zero (`withdraw` → `_burn` → `removeFromDepositTokensOfAccount`), so the freeze is temporary and self-healable once detected — consistent with a Medium-severity griefing finding. Existing balances are never frozen: `withdraw` and `transfer` of tokens already held still work.

### Likelihood Explanation
The attack is permissionless and requires no privileged role — `DepositToken.transfer` is a public ERC20 entry point and `addToDepositTokensOfAccount` is triggered automatically. Cost scales with the number of registered deposit tokens (capped at 30) and is limited to dust amounts of each underlying plus gas. Success depends on the victim not already holding ≥1 token and on the victim not noticing/clearing a slot before the targeted action; front-running the victim's transactions can extend the window.

### Recommendation
Apply a minimum-amount threshold before adding a token to an account's list (e.g., only add when the received amount exceeds a meaningful `minDeposit` value), or only add entries inside `deposit()`/`_mint` paths rather than on arbitrary `transfer`s — noting that `seize` during liquidation uses `_transfer` and must still register collateral for liquidators. Alternatively, exempt the "add" from reverting by capping tracked tokens differently (e.g., skip iteration-safe overflow handling instead of `UserReachedMaxTokens` revert on recipient-side adds).

### Proof of Concept
Hardhat sketch (deployment fixtures as in `test/Pool.test.ts`):

```ts
it('dust-fill victim token list blocks new deposits', async function () {
  const attacker = alice, victim = bob;
  const depositTokens = await pool.getDepositTokens(); // all registered DepositTokens

  // 1. Attacker deposits dust and transfers 1 wei of each DepositToken to victim
  for (const addr of depositTokens) {
    const dt = await ethers.getContractAt('DepositToken', addr);
    const underlying = await ethers.getContractAt('ERC20', await dt.underlying());
    const dust = await underlying.balanceOf(attacker.address); // obtain via faucet/swap
    await underlying.connect(attacker).approve(dt.address, dust);
    await dt.connect(attacker).deposit(dust, attacker.address);
    await dt.connect(attacker).transfer(victim.address, 1);
  }

  // pad with extra tokens if list < 30 (same pattern as Pool.test.ts max-tokens test)
  expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30);

  // 2. Any new-token deposit to victim reverts
  const newDeposit = await smock.fake('DepositToken'); // or next registered token not yet held
  // realistic variant: victim tries to deposit a collateral type not in their list
  await expect(
    someNewDepositToken.deposit(parseEther('1'), victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 3. issue() of a new debt token also reverts (addToDebtTokensOfAccount)
  await expect(
    newDebtToken.connect(victim).issue(parseEther('1'), victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```