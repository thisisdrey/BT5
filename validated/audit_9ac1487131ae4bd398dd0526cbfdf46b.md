### Title
Attacker can fill a victim's per-account token list with dust `DepositToken` transfers, blocking the victim from depositing new collateral types or minting new synthetics — (File: `contracts/Pool.sol`)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across the combined `depositTokensOfAccount` + `debtTokensOfAccount` lists. Because `DepositToken` is a freely transferable ERC20-style token and `_transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance moves from `0` to non-zero, an attacker can permissionlessly push dust amounts of every whitelisted deposit token into a victim's account. Once the victim's list hits 30 entries, `onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` on any subsequent `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` call — i.e. on any deposit of a collateral type the victim doesn't already hold, and on any `issue`/`mint` of a synthetic the victim doesn't already owe.

### Finding Description
Bug class (from the OpenQ report): an unprivileged attacker injects an attacker-chosen asset into shared per-position accounting such that legitimate user operations revert. The Metronome analog lives in the per-account token accounting rather than a bounty funding list:

- `Pool.addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148`, `204-220`).
- `DepositToken._transfer` adds the token to the **recipient's** list whenever `_recipientBalanceBefore == 0 && amount_ > 0`, with no opt-in by the recipient (`contracts/DepositToken.sol:517-520`). It removes from the sender's list if their balance hits zero.
- `DepositToken._mint` does the same on deposit (`contracts/DepositToken.sol:485-488`).
- `DebtToken` is non-transferable (`TransferNotSupported`, `contracts/DebtToken.sol:31`), so the debt side can't be stuffed directly — but `issue`/`mint` still routes through `addToDebtTokensOfAccount`, which is blocked once the combined list is full.
- `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` verify the **caller** is a registered token (`_revertIfSenderIsNotDepositToken` / `_revertIfSenderIsNotDebtToken`), so the attacker can't inject fake token addresses — but they don't need to: dust transfers of the real whitelisted `DepositToken`s are enough.

Attack path (unprivileged EOA):
1. Attacker deposits a small amount of each whitelisted collateral to obtain `msdTOKEN` balances (or buys/obtains them on secondary markets).
2. Attacker calls `DepositToken.transfer(victim, 1 wei)` for each deposit token. Each call adds that token to `depositTokensOfAccount[victim]`.
3. Once the victim's combined list reaches 30, any `deposit` into a collateral they don't already hold reverts (`addToDepositTokensOfAccount` → `UserReachedMaxTokens`), and any `issue` of a synthetic they don't already hold reverts the same way.
4. The attacker can front-run a victim's pending `deposit`/`issue` transaction with the dust transfer that fills the last slot, deterministically reverting it.

### Impact Explanation
Temporary freezing of victim funds / liveness: the victim is prevented from adding new collateral types and from minting new synthetic types while their list is saturated. This is especially damaging in liquidation scenarios — a victim whose position is deteriorating cannot deposit a different (e.g. more stable or better-priced) collateral to deleverage, so an attacker can force an otherwise-avoidable liquidation and capture the liquidation bonus. Existing deposits can still be withdrawn and existing positions repaid, and the victim can recover slots by transferring the dust `msdTOKEN`s to another address (burning via `withdraw` also removes entries), so the freeze is temporary and self-remediable at dust + gas cost. Note that while the attacker's transfer is subject to `_revertIfLocked` on the *sender* side, the dust landing on the victim is unrestricted.

### Likelihood Explanation
- Fully unprivileged: requires only holding/obtaining tiny `msdTOKEN` balances of the whitelisted deposit tokens and calling public `transfer`.
- No governor/keeper/oracle cooperation needed; no oracle manipulation; works on the deployed configuration (`MAX_TOKENS_PER_USER` is a hard-coded constant, same in mainnet/optimism/base/hemi deployments).
- Cost is bounded by the number of whitelisted deposit tokens (well under 30 in practice) — attacker only needs as many distinct tokens as needed to fill the victim's remaining slots, and can combine with existing entries.
- Caveat: impact is a temporary, user-remediable DoS rather than permanent loss; severity is moderate. The protocol intentionally caps the list to bound `debtPositionOf`/`depositOf`/`debtOf` loop gas, so this is a griefing vector against a deliberate invariant, not an unbounded-loop issue.

### Recommendation
- Track token list membership only on user-initiated `deposit`/`issue`, not on incoming `transfer`s — i.e. in `DepositToken._transfer`, do not call `addToDepositTokensOfAccount` for the recipient, or only add on the recipient's first *deposit* rather than any credit.
- Alternatively, keep additions on transfer but drop the `onlyIfAdditionWillNotReachMaxTokens` check from `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` and enforce the cap only in `deposit`/`issue`/`leverage` entry points (so unsolicited dust can never block an action the victim initiated).
- As a cheap mitigation, allow anyone to call a `removeFromDepositTokensOfAccount` for tokens with zero balance so slots can be freed without transferring dust.

### Proof of Concept
Hardhat sketch (against deployed-style fixtures):

```ts
// Assume: pool with >= N whitelisted DepositTokens deployed; victim has 29-n slots free.
it('dust fills victim token list and bricks new deposits/mints', async () => {
  const victim = bob.address;

  // 1) Attacker acquires dust of each deposit token
  for (const dt of depositTokens) {
    await underlying(dt).approve(dt.address, dustAmount);
    await dt.deposit(dustAmount); // attacker mints msdTOKEN to self
  }

  // 2) Grief: push dust to victim until list is full
  const max = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30
  let len = (await pool.getDepositTokensOfAccount(victim)).length
          + (await pool.getDebtTokensOfAccount(victim)).length;
  for (const dt of depositTokens) {
    if (len >= max) break;
    if ((await dt.balanceOf(victim)).isZero()) {
      await dt.transfer(victim, 1); // adds to depositTokensOfAccount[victim]
      len++;
    }
  }
  expect(len).to.eq(max);

  // 3) Victim deposit into a collateral they don't hold reverts
  const newDt = depositTokens.find(async (d) => (await d.balanceOf(victim)).isZero());
  await underlying(newDt).connect(bob).approve(newDt.address, amount);
  await expect(newDt.connect(bob).deposit(amount))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 4) Victim mint of a synthetic they don't owe reverts (addToDebtTokensOfAccount)
  await expect(debtToken.connect(bob).issue(amount))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // 5) Recovery requires victim actively transferring dust out
  await depositTokens[0].connect(bob).transfer(carol.address, 1);
});
```

Uncertainty note: I confirmed the add/remove bookkeeping (`DepositToken.sol:481-525`), the cap check (`Pool.sol:143-148`), and that `DebtToken` is non-transferable. I did not fully verify `DepositToken.transfer`'s sender-side `_revertIfLocked` path or whether `issue` adds to `debtTokensOfAccount` before or after health checks — the core griefing mechanism (permissionless recipient-side list insertion) is confirmed, but the exact revert ordering in `issue` should be validated when writing the runnable PoC.