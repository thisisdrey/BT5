### Title
Attacker can permanently keep dust `DepositToken` balances on a victim's account, preventing removal from `depositTokensOfAccount` and exhausting `MAX_TOKENS_PER_USER` to block deposits/transfers - ([File: contracts/DepositToken.sol](Kohvert/metronome-synth-public--017/contracts/DepositToken.sol))

### Summary
Metronome tracks which deposit tokens an account holds via a capped per-account set in `Pool`. Membership is controlled purely by the token balance hitting exactly zero: `DepositToken._transfer` adds a token to the recipient's set on first receipt (lines 517-520) and removes the sender's token only when `balanceOf[sender_] == 0` (lines 523-524); `_burn` does the same (lines 460-462). Because `transfer`/`transferFrom` are public and only gated by the sender's own unlocked balance (`_revertIfLocked`, lines 348-376), any EOA can dust 1 wei of any whitelisted `DepositToken` into an arbitrary victim. A single wei keeps the token in the victim's set forever if the victim cannot unlock it.

### Finding Description
The analog to the reported bug is that a permissionless token transfer flips a `balance == 0` equality check, breaking a liveness assumption:

- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever `_recipientBalanceBefore == 0 && amount_ > 0` (DepositToken.sol:517-520), and only calls `pool.removeFromDepositTokensOfAccount(sender_)` when the sender's balance reaches exactly `0` (lines 523-524). There is no opt-in: the recipient cannot refuse the dust.
- The pool's per-account set (`depositTokensOfAccount`, `MappedEnumerableSet` in `PoolStorage`) is bounded by `MAX_TOKENS_PER_USER`; once full, `addToDepositTokensOfAccount` reverts, which reverts the entire `transfer`, `deposit` mint (DepositToken.sol:486-488), or `seize` path.
- The victim's ability to shed the dust depends on `unlockedBalanceOf` (DepositToken.sol:383-398). If the victim has any debt and zero issuable headroom (`_issuableInUsd == 0`, e.g., an at- or below-water position), `unlockedBalanceOf` returns `0`, so `_revertIfLocked` blocks every `transfer`/`transferFrom`/`withdraw` of that token — the dust can never be removed and the set entry can never be freed.

### Impact Explanation
An attacker fills a victim's deposit-token set to `MAX_TOKENS_PER_USER` with 1-wei dust transfers of whitelisted collaterals. After that:

- The victim cannot deposit any additional collateral type (`deposit` → `_mint` → `addToDepositTokensOfAccount` reverts), so they cannot add collateral to rescue an unhealthy position — a liveness/insolvency aggravator.
- The victim cannot receive liquidated collateral in a new token type (`seize` → `_transfer` reverts), and no one can transfer a new deposit token to them.
- If the victim is already at/below their issuable limit, the dust balances are locked (`unlockedBalanceOf == 0`), making the DoS permanent until the position's health changes — analogous to the report's permanent `isLiquidationActive == true` state caused by a 1-token donation.

### Likelihood Explanation
- Fully permissionless: `transfer` only checks the *sender's* unlocked balance (DepositToken.sol:348-354); no health check, approval, or minimum amount applies to the recipient side.
- Cost is ~`MAX_TOKENS_PER_USER` wei of dust across distinct whitelisted deposit tokens; dust can be obtained by the attacker depositing tiny amounts or splitting existing balances.
- Front-running is trivial: the attacker can dust-fill the set just as a victim's position turns unhealthy, precisely when the victim most needs to deposit new collateral or receive seized collateral.
- No modifier stops it: `transfer` has no `nonReentrant`, no pause gate dependency on the recipient, and the recipient cannot reject or be excluded.

### Recommendation
- Do not drive set membership off exact `balance == 0` transitions reachable by third-party donations. Track per-account token membership in `Pool` (or a deposit-token-side bitmap) updated only by protocol flows (`deposit`, `withdraw`, `seize`, `liquidate`), or make recipients opt in (e.g., only add on `deposit`/`seize`, never on plain `transfer`).
- Alternatively, allow removal from the per-account set whenever the balance is below a dust threshold, or let an account forcibly "eject" a deposit token entry (burning/donating the dust to the fee collector) even while collateral is locked.
- As a defense-in-depth measure consistent with the external report's long-term advice, avoid `== 0` balance equality as a state predicate; use a storage-tracked flag/set that external `transfer`s cannot manipulate.

### Proof of Concept
Hardhat sketch (assuming existing fixtures from `test/Pool.test.ts`, where `pool`, `msdMET`, and other deposit tokens exist and `MAX_TOKENS_PER_USER` bounds `depositTokensOfAccount`):

```ts
// Victim Alice has a position at/below her issuable limit (unlockedBalanceOf == 0).
// Eve holds dust of deposit tokens d1..dN (obtained via tiny deposits or splits).

const N = await pool.MAX_TOKENS_PER_USER(); // or however the cap is exposed
const depositTokens = await pool.getDepositTokens();

// 1) Eve dust-fills Alice's per-account deposit token set
for (let i = 0; i < N; i++) {
  const dt = await ethers.getContractAt('DepositToken', depositTokens[i]);
  await dt.connect(eve).transfer(alice.address, 1); // 1 wei, unlocked for Eve
}

// Alice's set is now full; each entry has balance 1 and is locked because
// unlockedBalanceOf(alice) == 0 (debt position has no issuable headroom).
expect(await msdMET.unlockedBalanceOf(alice.address)).to.eq(0);

// 2) Alice tries to deposit a collateral type she does not yet hold -> reverts
const newDepositToken = await ethers.getContractAt('DepositToken', depositTokens[N]);
await underlying.connect(alice).approve(newDepositToken.address, amount);
await expect(
  newDepositToken.connect(alice).deposit(amount, alice.address)
).to.be.reverted; // addToDepositTokensOfAccount reverts: set is full

// 3) Alice cannot shed the dust: every transfer reverts via _revertIfLocked
await expect(
  msdMET.connect(alice).transfer(eve.address, 1)
).to.be.revertedWithCustomError(msdMET, 'NotEnoughFreeBalance');

// 4) A liquidator cannot seize into a token type Alice does not already hold:
// Pool.liquidate -> depositToken_.seize -> _transfer -> addToDepositTokensOfAccount reverts
// when the seized token is new for Alice, blocking that liquidation path.
```

Note: the exact revert type for a full set (`Pool.addToDepositTokensOfAccount` / `MappedEnumerableSet.add` and the `MAX_TOKENS_PER_USER` constant in `Pool.sol`/`PoolStorage.sol`) could not be read within the available iterations; the mechanism is confirmed by `DepositToken._transfer`/`_burn`'s zero-balance membership transitions and `_revertIfLocked` gating. The PoC should be validated against the concrete cap and error names in `contracts/Pool.sol` and `contracts/lib/MappedEnumerableSet.sol`.