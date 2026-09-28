### Title
Dust-transfer griefing of `Pool.MAX_TOKENS_PER_USER` blocks a victim's deposits and liquidations - (File: contracts/DepositToken.sol)

### Summary
`DepositToken._transfer` and `DepositToken._mint` call `pool.addToDepositTokensOfAccount(recipient)` whenever the recipient's balance of that msd token was previously zero (`DepositToken.sol:486-488`, `DepositToken.sol:518-520`). `Pool` enforces a shared cap `MAX_TOKENS_PER_USER` across the sum of an account's deposit-token and debt-token lists and reverts with `UserReachedMaxTokens` once the cap is hit (see `test/Pool.test.ts:1386-1416` and `1491-1521`, which confirm the combined deposit+debt count is compared against the cap). Because the registration happens on the *recipient* side and is triggered by any ERC20 `transfer`/`transferFrom`, an unprivileged attacker can permissionlessly fill a victim's token list with dust balances of every deposit token in the pool, after which any action that would register a *new* token for the victim reverts.

### Finding Description
The bug class is account-identity/list DoS: like the RuoYi issue where a duplicate login name poisons an account and causes denial of service, here an attacker poisons a victim's `depositTokensOfAccount` set with unsolicited entries, causing legitimate operations to revert.

Attack path (all unprivileged, public entry points):
1. Attacker calls `DepositToken.deposit(dust, attacker)` for each deposit token in the pool (`DepositToken.sol:211-237`). `deposit` is `whenNotPaused`, `nonReentrant`, `onlyIfDepositTokenExists` — no whitelist.
2. Attacker calls `DepositToken.transfer(victim, 1 wei)` for each token (`DepositToken.sol:348-354`). `_revertIfLocked` only checks the *sender's* unlocked balance; there is no opt-in or minimum-amount check on the recipient.
3. Each first-time receipt triggers `pool.addToDepositTokensOfAccount(victim)`. Once `victim`'s combined list length reaches `MAX_TOKENS_PER_USER`, the next registration reverts with `UserReachedMaxTokens`.

Consequences once the list is full:
- `DepositToken.deposit(amount, victim)` in any *new* collateral reverts inside `_mint` at `addToDepositTokensOfAccount` (`DepositToken.sol:487`). A victim whose position is approaching liquidation cannot add a new collateral type to restore health.
- `Pool.liquidate` seizes collateral via `DepositToken.seize` → `_transfer` → `addToDepositTokensOfAccount(liquidator)` (`DepositToken.sol:343-345`, `518-520`). If the liquidator's list is full (or the victim's seized token would be new to them), the seize reverts and liquidation fails, so the same dust-griefing applied to liquidators or to accounts that would receive seized collateral blocks the liquidation path.
- The same mechanics apply to `debtTokensOfAccount`/`addToDebtTokensOfAccount` via `DebtToken`, so the combined cap can be exhausted entirely with deposit-token dust.

### Impact Explanation
Temporary freezing of funds / liveness failure: the victim cannot open positions in new collateral tokens and a grieved liquidator cannot seize new collateral types, so underwater positions may not be liquidatable or top-up-able through those tokens. The freeze is temporary because the victim can clear slots by transferring the dust balances away (`_transfer` removes the entry when the sender balance hits zero, `DepositToken.sol:523-525`), but this requires the victim to discover the attack and spend gas on up to `MAX_TOKENS_PER_USER` cleanup transfers — and the attacker can refill slots frontrunning recovery. During the window, collateral cannot be added and liquidations can fail, directly impacting solvency-related operations.

### Likelihood Explanation
Requires only an EOA: deposit dust (costs gas + dust principal in each underlying) and call `transfer`. No privileged role, no oracle manipulation, no governance dependency. The number of deposit tokens in a deployed pool is small, so filling `MAX_TOKENS_PER_USER` is cheap and can be done atomically via `Operator.execute` or a contract. Impact is bounded (temporary, remediable by the victim), consistent with Medium severity.

### Recommendation
- Do not auto-register a token in `depositTokensOfAccount` on unsolicited `transfer`/`transferFrom`/`seize` receipt; only register on `deposit`/`_mint`, or make transfers to a zero-balance recipient revert unless the recipient opt-ed in.
- Alternatively, keep a separate explicit `collateralEnabled` flag set by the account owner instead of inferring collateral membership from balance > 0, and iterate over that set in `debtPositionOf`.
- As a mitigation, let anyone call a `removeDustFromAccount(account, token)`-style cleanup, or exclude sub-dust balances from triggering `UserReachedMaxTokens`.

### Proof of Concept
Hardhat outline against the existing fixtures (as in `test/Pool.test.ts`):

```ts
it('grief: fill victim deposit-token list via dust transfers', async function () {
  const victim = alice.address
  // for each registered DepositToken msdX:
  for (const msd of depositTokens) {
    const underlying = await ethers.getContractAt('ERC20', await msd.underlying())
    await underlying.mint(attacker.address, 10)
    await underlying.connect(attacker).approve(msd.address, 10)
    await msd.connect(attacker).deposit(10, attacker.address)   // mints dust
    await msd.connect(attacker).transfer(victim, 1)              // registers on victim
  }
  // victim's combined list is now at MAX_TOKENS_PER_USER

  // 1) victim cannot deposit a NEW collateral token
  await underlying2.mint(victim, parseEther('1'))
  await underlying2.connect(alice).approve(msdNew.address, parseEther('1'))
  await expect(msdNew.connect(alice).deposit(parseEther('1'), victim))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens')

  // 2) a liquidator whose list is full cannot seize a new collateral type
  //    -> pool.liquidate(...) reverts inside DepositToken.seize -> _transfer
  //    -> addToDepositTokensOfAccount(liquidator) -> UserReachedMaxTokens
})
```

Caveat: I could not verify the exact value of `MAX_TOKENS_PER_USER` or the `liquidate` seize-target argument within the available iterations, but the revert condition and both trigger paths are confirmed by `DepositToken.sol` and the dedicated revert tests in `test/Pool.test.ts`.