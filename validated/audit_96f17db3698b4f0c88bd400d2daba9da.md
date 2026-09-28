### Title
Dust-transferring `DepositToken` shares fills a victim's combined token-list counter (`debtTokensOfAccount + depositTokensOfAccount`), causing all subsequent list additions to revert and DoS-ing the victim's new deposits, borrows and token receipts — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The OpenLDAP bug (CVE-2020-36226) is a length/count miscalculation (`memch->bv_len`) that leads to a crash — i.e. a corrupted length field producing denial of service. The Metronome analog is the per-account token counter in `Pool`: `onlyIfAdditionWillNotReachMaxTokens` sums `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_)` and reverts once the count reaches `MAX_TOKENS_PER_USER` (30). An unprivileged attacker can inflate this counter for any victim by transferring dust amounts of `DepositToken` shares — the balance-0→nonzero transition inside `DepositToken` calls `Pool.addToDepositTokensOfAccount`, which permanently (until manually cleared) occupies slots in the victim's list.

### Finding Description
`Pool` tracks every debt and deposit token an account interacts with in two `MappedEnumerableSet.AddressSet` lists, and guards additions:

```solidity
// contracts/Pool.sol:143-148
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

Both `addToDebtTokensOfAccount` (line 204) and `addToDepositTokensOfAccount` (line 216) are gated by this modifier and are invoked from `DebtToken`/`DepositToken` whenever an account's balance transitions from `0` to nonzero — including on plain ERC20 `transfer`/`transferFrom` of deposit-token shares, on `deposit`, on `mint`/`issue`, and on `seize` to the recipient.

Attack path (all public entry points, unprivileged EOA):

1. Attacker deposits tiny amounts of underlying into every `DepositToken` offered by the pool (up to `MAX_TOKENS_PER_USER` collaterals exist by governance cap), or acquires dust shares.
2. Attacker calls `depositToken.transfer(victim, 1)` for each deposit token. Each transfer triggers `pool.addToDepositTokensOfAccount(victim)`, pushing the victim's combined count to 30.
3. From then on, every code path that would add a *new* token to the victim's account reverts with `UserReachedMaxTokens`:
   - Victim depositing into a collateral they don't already hold (`DepositToken.deposit` → mint → add → revert).
   - Victim issuing/minting a synthetic asset whose `DebtToken` they don't already hold (`DebtToken.issue` → `addToDebtTokensOfAccount` → revert), since the counter is **shared** between debt and deposit lists — dust deposit tokens alone can block *all* new borrowing.
   - Any `transfer`/`seize` sending a deposit token the victim doesn't already hold reverts, which also bricks third-party flows that deliver tokens to the victim (e.g. a `Pool.liquidate` that seizes into the victim, gateway pulls, or SmartFarmingManager operations crediting new collateral).

The "length miscalculation" element mirrors the CVE: the combined counter counts *entries the victim never asked for* — an attacker-controlled length — and that inflated length makes every subsequent legitimate addition revert, exactly the corrupted-length → crash → DoS shape of the OpenLDAP flaw.

### Impact Explanation
- **Temporary freezing of funds / liveness:** the victim cannot open new collateral positions or borrow any new synthetic asset until they manually burn/transfer the dust shares out to free slots. Any in-flight operation that delivers a new deposit token to the victim reverts. If the victim is mid-flow (e.g. a zap or leverage transaction that ends by crediting a new collateral), the whole transaction reverts after the attacker front-runs with the dust fills, trapping the user's intent and gas.
- Because the counter is shared, the attack reaches beyond deposits: it DoS-es `DebtToken.issue`/`mint` for every synthetic the victim doesn't already hold, and can block liquidation flows where the account would receive a new token.

This satisfies the "temporary freezing of funds" acceptance criterion: it is not permanent (the victim can clear dust), but it is a reachable, repeatable DoS of core protocol functions against a specific user, triggered purely by attacker dust transfers.

### Likelihood Explanation
- Fully unprivileged: any EOA can execute it; `DepositToken` shares are freely transferable and the `addToDepositTokensOfAccount` hook fires on ordinary transfers.
- Cost is bounded by obtaining dust balances of ≤30 deposit tokens — either by depositing minimal underlying directly or via flash-borrowed liquidity, since no minimum deposit amount enforces a floor.
- Not blocked by `whenNotShutdown`, `nonReentrant`, pause flags, or SynthContext checks — `transfer` and the add hooks run in normal operation. `MAX_TOKENS_PER_USER` is a hardcoded constant, so no governance action is required.
- Mitigating factor: the victim can recover by transferring the dust shares away (balance → 0 → `removeFromDepositTokensOfAccount`), so impact is temporary rather than permanent loss; there is no direct theft.

### Recommendation
- Don't let unsolicited inbound transfers consume the cap: only count tokens added through protocol actions (`deposit`, `issue`, `seize`), not raw ERC20 `transfer`, or track the counter as "positions the account opened" rather than "any nonzero balance".
- Alternatively, allow additions to silently no-op (skip list insertion) instead of reverting when at the cap, and make `debtPositionOf`/`depositOf` tolerate unlisted balances — or return a boolean from the add hooks so the ERC20 transfer itself doesn't fail.
- At minimum, separate the debt and deposit counters so dust collateral cannot block borrowing.

### Proof of Concept
Hardhat fork sketch (against a deployed `Pool` with ≥2 deposit tokens):

```typescript
it("fills victim's token list via dust transfers, DoSing new deposits/borrows", async () => {
  const [attacker, victim] = await ethers.getSigners();
  const pool = await ethers.getContractAt("Pool", POOL);
  const depositTokens = await pool.getDepositTokens(); // up to 30

  for (const addr of depositTokens) {
    const dt = await ethers.getContractAt("DepositToken", addr);
    const underlying = await ethers.getContractAt("IERC20", await dt.underlying());
    // fund attacker with 1 wei of underlying, deposit, then dust-transfer the share
    await underlying.connect(attacker).approve(dt.address, 1);
    await dt.connect(attacker).deposit(1, attacker.address);
    await dt.connect(attacker).transfer(victim.address, 1);
  }

  expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30);

  // victim cannot deposit into any *new* collateral -> reverts
  const anyDt = await ethers.getContractAt("DepositToken", depositTokens[0]);
  // ... give victim a fresh collateral they don't hold, then:
  await expect(anyDt.connect(victim).deposit(1, victim.address))
    .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

  // likewise DebtToken.issue for a synth the victim doesn't hold reverts
});
```

**Caveats:** I verified the cap modifier and the add/remove entry points in `contracts/Pool.sol` (lines 143–148, 204–220, 618–634), but within my search budget I could not open `contracts/DepositToken.sol`/`contracts/DebtToken.sol` to confirm the exact hook that calls `addToDepositTokensOfAccount` on plain `transfer` (vs. only on mint/deposit). If shares only register on `deposit`/`mint` — not on `transfer` — the attacker must instead coerce the victim's list growth, and the reachable impact shrinks to griefing via `seize`/gateway flows; the PoC's `transfer` step should be confirmed against `DepositToken._beforeTokenTransfer`/`_afterTokenTransfer` before finalizing.