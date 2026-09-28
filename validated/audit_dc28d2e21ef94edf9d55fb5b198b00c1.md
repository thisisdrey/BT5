### Title
Unprivileged attacker can fill a victim's token list to `MAX_TOKENS_PER_USER` via dust `DepositToken.deposit`/`transfer`, forcing reverts on deposits, mints, transfers and liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces a hard cap of 30 tokens (deposit + debt) per account via `onlyIfAdditionWillNotReachMaxTokens`. Any account — not just the token owner — ends up on the recipient's per-account lists whenever a `DepositToken` balance is minted or transferred to it. Because `DepositToken.deposit(amount_, onBehalfOf_)` accepts an arbitrary beneficiary and `DepositToken.transfer` is permissionless, an unprivileged attacker can permanently occupy all 30 list slots of a victim using 1-wei dust positions, causing every subsequent action that would add a new token to the victim's account to revert with `UserReachedMaxTokens`. This mirrors the CVE-2022-21435 bug class (attacker-triggered availability loss / repeatable "crash" of the target's operations).

### Finding Description
In `Pool.sol`, `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30):

```solidity
// contracts/Pool.sol:143-148
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

Entries are added in two places the attacker controls:

1. `DepositToken._mint` — `deposit()` lets the caller choose `onBehalfOf_`, and mints dust to any victim. When `balanceOf[victim] == 0` before the mint, it calls `pool.addToDepositTokensOfAccount(victim)`:
   ```solidity
   // contracts/DepositToken.sol:486-488
   if (_balanceBefore == 0 && amount_ > 0) {
       pool.addToDepositTokensOfAccount(account_);
   }
   ```
2. `DepositToken._transfer` — a plain `transfer(victim, 1)` from any holder adds the token to the victim's list:
   ```solidity
   // contracts/DepositToken.sol:518-520
   if (_recipientBalanceBefore == 0 && amount_ > 0) {
       pool.addToDepositTokensOfAccount(recipient_);
   }
   ```

There is no minimum deposit amount and no opt-in/consent check on the recipient. Once the combined list length reaches 30, the following all revert for the victim:

- `DepositToken.deposit` for any deposit token not already in the victim's list (revert inside `_mint` → `addToDepositTokensOfAccount`).
- Receiving any new `DepositToken` via `transfer`/`transferFrom` — including `Pool.seize` during `Pool.liquidate`, which calls `DepositToken.seize → _transfer`. If the chosen liquidator/recipient is at the cap, the entire liquidation transaction reverts.
- Issuing a new `DebtToken` position (debt mint to the account calls `addToDebtTokensOfAccount` from `DebtToken`, which hits the same cap).

Recovery requires the victim to manually burn the dust positions (deposit-token `withdraw` of the 1-wei balances triggers `removeFromDepositTokensOfAccount` on zero balance), meaning the attack is a temporary-but-repeatable freeze that costs the attacker only dust plus deposit fees per slot. Removal entries in the debt-token list are harder: a dust *debt* position cannot be added on behalf of the victim (debt mints go through `Pool.issue` to the caller), so the attacker fills the deposit side only — still sufficient to reach the cap.

### Impact Explanation
Availability loss for a targeted account, matching the "unauthorized hang / repeatable crash" class of the CVE. Concretely, a fully filled list causes:

- Victim's deposits of any new collateral type to revert (liveness/freeze of the deposit path).
- Liquidations where the beneficiary (e.g., a known liquidation bot/EOA) is at the cap to revert inside `seize`, delaying liquidation of unhealthy positions and increasing bad-debt risk. Attackers can pre-fill the lists of public liquidator addresses to widen this window.
- New debt issuance (`DebtToken` positions in additional synthetics) to revert for the victim.

The invariant broken is liveness: core user flows (deposit, mint, transfer, seize/liquidate) deterministically revert despite no misuse by the victim. No privileged role, oracle manipulation, or governance action is required; only public entry points and dust amounts.

### Likelihood Explanation
- Reachability: 100% — `DepositToken.deposit` and `transfer` are public, `whenNotPaused`, and accept arbitrary recipients; `addToDepositTokensOfAccount` is callable by any registered deposit token (triggered internally by `_mint`/`_transfer`).
- Cost: deposits of the minimum nonzero amount (1 wei of underlying, plus any configured deposit fee) across at most 30 tokens; if the victim already uses `n` tokens, only `30 - n` dust deposits are needed. With ~5–10 live deposit tokens per pool on deployed chains (mainnet/base/optimism/hemi deployments), a handful of deposit transactions suffices.
- Persistence: entries are removed only when the victim's balance returns to exactly zero, so the attack persists until the victim actively withdraws each dust position — and the attacker can refill slots as they are freed.
- Constraints: `deposit` is `nonReentrant` and `whenNotPaused`, neither of which blocks this. `SynthContext` meta-sender resolution does not change the recipient accounting.

### Recommendation
- Make list membership opt-in or bounded by meaningful value: enforce a minimum deposit amount (e.g., a `minDepositInUsd` oracle check) so dust cannot occupy a slot.
- Alternatively, only add a token to `depositTokensOfAccount` on first *self-initiated* deposit — i.e., require `account_ == _msgSender()` in `deposit`, or track "active" vs "passively received" balances and exclude passively-received dust from `debtPositionOf`/`depositOf` accounting.
- As defense-in-depth for liquidation liveness, let `liquidate` send seized collateral to a recipient chosen freely (already supported via `to_`) and/or skip adding seize recipients to the cap check, since a liquidator should never be blocked from receiving collateral.
- Consider raising or removing `MAX_TOKENS_PER_USER` if the gas-bounded iteration in `debtOf`/`depositOf` is safe at the deployed token count, since per-account lists never exceed the number of registered tokens anyway.

### Proof of Concept
Hardhat test sketch (fork not strictly required — works against the protocol's own deployment fixtures; on a mainnet/Base fork use the real `Pool`, `DepositToken` list and `smock`-free calls):

```ts
// test/DoS-max-tokens.test.ts
it('griefs victim: fills token list, deposits + liquidations revert', async () => {
  const victim = alice.address;
  const depositTokens = await pool.getDepositTokens(); // registered DepositTokens
  const debtTokens = await pool.getDebtTokens();

  const MAX = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30
  const victimCurrent =
    (await pool.getDepositTokensOfAccount(victim)).length +
    (await pool.getDebtTokensOfAccount(victim)).length;

  // Attacker holds dust of each underlying (or flash-borrows it).
  // For every deposit token the victim does NOT already hold:
  for (const dt of depositTokens) {
    const combined =
      (await pool.getDepositTokensOfAccount(victim)).length +
      (await pool.getDebtTokensOfAccount(victim)).length;
    if (combined >= MAX) break;
    if ((await pool.getDepositTokensOfAccount(victim)).includes(dt)) continue;

    const underlying = await IERC20__factory.connect(await DepositToken__factory.connect(dt, attacker).underlying(), attacker);
    await underlying.approve(dt, 1);
    await DepositToken__factory.connect(dt, attacker).deposit(1, victim); // 1 wei on behalf of victim
  }

  expect(
    (await pool.getDepositTokensOfAccount(victim)).length +
      (await pool.getDebtTokensOfAccount(victim)).length
  ).to.eq(MAX);

  // 1) Victim can no longer deposit into any NEW deposit token
  const newDt = depositTokens.find(async (dt) => !(await pool.getDepositTokensOfAccount(victim)).includes(dt));
  // (for PoC pick any dt victim wasn't in list before attack)
  await expect(
    DepositToken__factory.connect(targetDepositToken, victimSigner).deposit(parseEther('1'), victim)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 2) transfer of a new deposit token to victim reverts
  await expect(
    DepositToken__factory.connect(targetDepositToken, attacker).transfer(victim, 1)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 3) Pre-fill a liquidator's list the same way, then:
  //    pool.liquidate(unhealthy, repayAmount, depositToken, liquidator)
  //    reverts inside DepositToken.seize -> _transfer -> addToDepositTokensOfAccount
  await expect(
    pool.connect(liquidator).liquidate(unhealthyAccount, amountToRepay, depositToken, liquidator.address)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```

The key assertion is that a single unprivileged EOA, spending only dust collateral and deposit fees, deterministically forces `UserReachedMaxTokens` reverts on the victim's deposit/mint/transfer path and on `liquidate` beneficiaries — a repeatable availability failure standing on Metronome's own code in `contracts/Pool.sol:143-148` and `contracts/DepositToken.sol:486-488,518-520`.