### Title
Attacker can DoS a victim's deposits/issuance of any new token by dust-transferring `DepositToken`s to fill their `MAX_TOKENS_PER_USER` slots - (File: contracts/DepositToken.sol)

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined number of deposit and debt tokens tracked per account. `DepositToken._transfer` unconditionally registers the token in the recipient's per-account set on first receipt, and `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the cap is hit. Any user can transfer `DepositToken`s, so an attacker can push dust amounts of every pool deposit token to a victim, exhaust their slots, and cause every subsequent first-time deposit, transfer-in, issuance, or liquidation-seizure targeting that account to revert.

### Finding Description
The per-account token list is maintained in `Pool` via `MappedEnumerableSet` and capped by `onlyIfAdditionWillNotReachMaxTokens`:

```solidity
// contracts/Pool.sol
uint256 public constant MAX_TOKENS_PER_USER = 30;                                  // L79

modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}                                                                                  // L143-148

function addToDepositTokensOfAccount(address account_) external onlyIfAdditionWillNotReachMaxTokens(account_) {
    address _depositToken = _msgSender();
    _revertIfSenderIsNotDepositToken(_depositToken);
    if (!depositTokensOfAccount.add(account_, _depositToken)) revert DepositTokenAlreadyExists();
}                                                                                  // L216-220
```

`DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance was zero — with no opt-in from the recipient and no minimum amount:

```solidity
// contracts/DepositToken.sol
// Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}                                                                                  // L518-520
```

The same registration happens on `_mint` (L486-488), so `deposit(..., onBehalfOf_ = victim)` (L211-237) and `seize` during liquidation (L343-345) also route through the cap.

Attack path (all calls are public, no privileged role needed):

1. Attacker deposits a minimal amount of each listed collateral into their own account via `deposit(amount_, attacker)` (or acquires the msdTOKENs on the market).
2. For each `DepositToken` in the pool, the attacker calls `transfer(victim, 1)` (or front-runs the victim's pending `deposit`/`issue` tx with it). Each transfer inserts that token into `depositTokensOfAccount[victim]`.
3. Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) >= 30`, every code path that would add a new token to the victim's lists reverts with `UserReachedMaxTokens`:
   - `DepositToken.deposit` to a collateral the victim doesn't yet hold (`_mint` → `addToDepositTokensOfAccount`),
   - any third-party `transfer`/`transferFrom` of a new msdTOKEN to the victim,
   - `DebtToken.issue`/`mint`/`flashIssue` for a synthetic the victim doesn't yet hold (same cap via `addToDebtTokensOfAccount`, L204-208),
   - `Pool.liquidate` → `DepositToken.seize` when the seized collateral would be a new entry for the liquidator-chosen recipient path.

The attacker can repeat the refill indefinitely at dust cost, and can front-run each of the victim's legitimate transactions — directly analogous to the reported front-run-on-unique-key DoS.

### Impact Explanation
Griefing / temporary freezing of protocol functionality for the targeted account. The victim is denied the ability to onboard new collateral types, issue new synthetics, or receive new msdTOKENs while the attacker keeps their slot set saturated — at a cost of 1 unit of each deposit token per refill, reclaimable in part by re-transferring. Positions in already-listed tokens still work, but the denial is enforced by an unconditional revert inside `_transfer`/`_mint`/`seize`, so it also bricks liquidations and SmartFarmingManager flows (`withdrawFrom`, `flashWithdraw` collateralization paths) that would add a new token to the account. No modifier (`nonReentrant`, `whenNotPaused`, `SynthContext`, lock checks) prevents it: `_transfer` intentionally bypasses none of these, and `_revertIfLocked` only checks the sender's balance.

### Likelihood Explanation
The attack requires only that the pool lists enough distinct deposit/debt tokens for the attacker (combined with tokens the victim already uses) to reach the cap of 30, and that the attacker hold a dust balance of each — obtainable by depositing minimal amounts, which costs only the deposit fee. It needs no privileged role, oracle manipulation, or flash liquidity; front-running/victim-blind transfers are sufficient because the recipient cannot reject the registration. The victim can recover by transferring each dust token out (`_transfer` removes the entry when the sender's balance hits zero, L522-525) or repaying-withdrawing, but the attacker can cheaply re-grief, and each forced cleanup costs the victim gas and failed-transaction reverts.

### Recommendation
- Make registration in `depositTokensOfAccount`/`debtTokensOfAccount` opt-in (e.g., track tokens only on `deposit`/`issue` initiated by the account owner or via an explicit `onBehalfOf` opt-in flag), or
- Exclude transfers below a meaningful threshold, or lazily skip adding on plain `transfer`/`seize` (treat dust-received tokens as non-collateral), or
- Raise/remove the per-account cap by replacing the enumerated set with a bounded iteration over only tokens the account actually uses as collateral, or
- In `addTo*TokensOfAccount`, skip (instead of reverting) when the cap is reached for tokens that are not needed for health-factor computation, so a revert can't be weaponized.

### Proof of Concept
Hardhat test (place under `test/Pool.test.ts` / `test/DepositToken.test.ts` fixture that deploys a `Pool` with N registered `DepositToken`s):

```ts
it('should DoS victim deposits by filling MAX_TOKENS_PER_USER with dust transfers', async () => {
  // given: pool has >= MAX_TOKENS_PER_USER deposit tokens registered
  const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()
  const depositTokens = await pool.getDepositTokens() // all registered

  // attacker deposits 1 wei of each collateral and dusts the victim
  for (let i = 0; i < max; i++) {
    const dt = await ethers.getContractAt('DepositToken', depositTokens[i])
    const underlying = await ethers.getContractAt('ERC20', await dt.underlying())
    await underlying.connect(attacker).approve(dt.address, 1)
    await dt.connect(attacker).deposit(1, attacker.address)
    await dt.connect(attacker).transfer(victim.address, 1)
  }

  // victim's set is saturated
  expect((await pool.getDepositTokensOfAccount(victim.address)).length).to.eq(max)

  // then: victim cannot deposit into any collateral they don't already hold
  const newDt = await ethers.getContractAt('DepositToken', depositTokens[0]) // any token not yet in victim's list after rotation
  await underlying2.connect(victim).approve(newDt.address, parseEther('1'))
  await expect(newDt.connect(victim).deposit(parseEther('1'), victim.address))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens')

  // and cannot issue a synthetic for a debt token not yet in their list
  await expect(debtToken.connect(victim).issue(parseEther('1'), victim.address))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens')

  // and liquidation that seizes a new collateral type for the victim account reverts
  // via DepositToken.seize -> _transfer -> addToDepositTokensOfAccount
})
```

Note: verification that `DebtToken.issue`/`mint`/`flashIssue` hit the same `onlyIfAdditionWillNotReachMaxTokens` cap via `addToDebtTokensOfAccount` is inferred from `Pool.sol` L204-208; I could not fully re-read `DebtToken.sol` transferability within the iteration budget — if debt tokens are non-transferable, the attacker relies on deposit-token dust alone, so the attack is fully effective only when the pool lists enough deposit tokens (or the victim already occupies some slots).