### Title
Dust-transfer griefing fills a victim's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER`, permanently blocking the victim from depositing new collateral or receiving new deposit tokens - (File: contracts/DepositToken.sol)

### Summary
Analogous to the Argo CD repo-server DoS — where a low-privileged user supplies an archive whose extracted contents the server cannot reject or clean up — Metronome lets any unprivileged holder of a `DepositToken` push an entry into *another* account's bounded per-account token list via an ordinary ERC20 transfer. `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to >0, with no opt-in or minimum amount. Since `Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` across `debtTokensOfAccount + depositTokensOfAccount` (`onlyIfAdditionWillNotReachMaxTokens`), an attacker can dust-transfer 1 wei of every registered deposit token to a victim, exhausting the cap. After that, every code path that would add a new deposit token for the victim reverts with `UserReachedMaxTokens`, including `deposit(onBehalfOf_ = victim)`, `seize`, and inbound transfers.

### Finding Description
- `DepositToken._transfer` (contracts/DepositToken.sol:498-526) unconditionally adds the token to the recipient's list on first receipt (line 518-519). `transfer`/`transferFrom` only check the *sender's* unlocked balance, so anyone holding a deposit token (attacker deposits once via `deposit(amount_, attacker)`) can send dust to arbitrary victims.
- `Pool.addToDepositTokensOfAccount` (contracts/Pool.sol:216-220) is gated by `onlyIfAdditionWillNotReachMaxTokens` (line 143-148), which reverts once the combined debt+deposit token count reaches 30.
- Number of attacker-controlled tokens is bounded only by the number of deposit/debt tokens registered in the pool; pools list many collaterals, and the victim's own existing positions count toward the cap, so filling the remaining slots is cheap.
- Same mechanism applies to debt: any call that first-mints a `DebtToken` to the victim also reverts, blocking `DebtToken.issue`/`mint` on behalf of the victim (i.e., borrowing or leveraged top-ups via `SmartFarmingManager`).

### Impact Explanation
Temporary freezing of funds / forced-liquidation griefing — invariant broken: liveness. A victim whose position is drifting toward liquidation cannot deposit a *new* collateral type to restore health (every `deposit` of an unheld token reverts). If their health can only be saved by adding a new collateral or if existing collateral CF/price deteriorates, the position is liquidated while the victim is actively prevented from rescuing it. Liquidation itself is not blocked (seize credits the *liquidator*, not the victim), so the attacker converts a solvency-management denial into a real loss for the victim. The victim can recover by transferring/withdrawing dust balances to zero to free slots, but each removal requires a separate transaction the attacker can re-grief cheaply, mirroring the advisory's "undeletable extracted files" trait.

### Likelihood Explanation
Fully unprivileged: the attacker needs only a deposit position (or a friendlier holder) and dust amounts of the pool's deposit tokens. No governance, oracle, or privileged role required. Works on any deployed pool configuration; pause flags, reentrancy guards, and SynthContext checks don't interfere because `transfer` is a plain public ERC20 entry point. The cost is bounded by (~30 × transfer gas + dust principal), and re-griefing after victim cleanup costs one transfer per token.

### Recommendation
- Require recipient opt-in for list insertion, or only track tokens deposited via `deposit`/`seize` rather than arbitrary `transfer` (i.e., remove the `addToDepositTokensOfAccount` hook from `_transfer` and keep it in `_mint`/`seize` paths only).
- Alternatively, implement a global min-transfer amount or let `addToDepositTokensOfAccount` fail-open (skip tracking instead of reverting) when the cap is hit, since a dust balance contributes negligible collateral anyway.
- Allow a "sweep" escape hatch: a function that force-removes tokens with balance below a dust threshold from an account's list.

### Proof of Concept
Hardhat sketch (extend `test/Pool.test.ts` fixtures):

```ts
// Attacker holds each deposit token (e.g. msdWETH, msdWBTC, ...) via small deposits.
// Victim already uses N slots; attacker fills the rest.
const tokens = [msdWETH, msdWBTC, /* ... all registered deposit tokens */];
for (const t of tokens) {
  await t.connect(attacker).transfer(victim.address, 1); // dust; victim balance 0 -> 1
}

// Victim's list is now at MAX_TOKENS_PER_USER (30)
expect((await pool.getDepositTokensOfAccount(victim.address)).length
  + (await pool.getDebtTokensOfAccount(victim.address)).length).eq(30);

// Victim cannot deposit a collateral type they don't already hold:
await underlyingNew.mint(victim.address, parseEther('10'));
await underlyingNew.connect(victim).approve(msdNew.address, parseEther('10'));
await expect(
  msdNew.connect(victim).deposit(parseEther('1'), victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Any protocol path minting a new deposit/debt token to the victim
// (leverage, seize-to-victim, airdrop of deposit tokens) also reverts,
// so a deteriorating position cannot be rescued with new collateral
// until the victim burns/withdraws each dust balance to zero.
```

Note the attacker can front-run the victim's cleanup (`withdraw` to zero) with a fresh 1-wei transfer, making the denial persistent for as long as they keep spending one cheap transfer per slot.