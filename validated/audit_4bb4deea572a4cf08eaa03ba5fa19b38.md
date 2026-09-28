### Title
Attacker fills a victim's per-account token list to `MAX_TOKENS_PER_USER` via dust deposits/transfers, blocking new collateral deposits and synthetic issuance - ([File: contracts/Pool.sol])

### Summary
Metronome tracks, per account, every `DepositToken` and `DebtToken` the account holds in `depositTokensOfAccount`/`debtTokensOfAccount` (`MappedEnumerableSet`). A hard cap `MAX_TOKENS_PER_USER = 30` is enforced in `Pool.onlyIfAdditionWillNotReachMaxTokens`, which guards `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` (Pool.sol:143-148, 204-220). Entries are added whenever an account's balance of a token goes from 0 to >0 — via `DepositToken._mint` (on `deposit`), `DepositToken._transfer` (on `transfer`/`transferFrom`), and the analogous `DebtToken` mint path — and are only removed when the balance returns to exactly 0. Because `DepositToken.deposit(amount_, onBehalfOf_)` and `DepositToken.transfer` let anyone credit an arbitrary recipient, an unprivileged attacker can cheaply fill a victim's 30 slots with dust, after which every operation that would add a *new* token to the victim's lists reverts with `UserReachedMaxTokens`.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (Pool.sol:143-148, 204-220).
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` on the recipient's first receipt of a token, with no opt-in (DepositToken.sol:517-520).
- `DepositToken._mint` does the same on `deposit(amount_, onBehalfOf_)`, so an attacker needs only the underlying ERC20 — not existing msdTOKEN balances — to push entries into a victim's set (DepositToken.sol:485-488).
- Removal only happens when balance hits 0 (`removeFromDepositTokensOfAccount`), i.e., only the victim can clean up, and only by fully zeroing each dust balance.
- Once the combined list reaches 30, the following victim operations revert deterministically:
  - `DepositToken.deposit` of any collateral type the victim doesn't already hold (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`). This includes `NativeTokenGateway.deposit` for the native-token msdTOKEN.
  - `DebtToken` issuance/`flashIssue`/SmartFarmingManager leverage into any synthetic the victim doesn't already hold (`_mint` → `addToDebtTokensOfAccount`).
  - Receiving any new msdTOKEN/`DebtToken` via `transfer`/`transferFrom`, including `DepositToken.seize` proceeds if the victim were the liquidator.
- No modifier stops this: `deposit`/`transfer` only check `isActive`, lock/health checks apply to the *sender*, and `addTo*` only verifies `msg.sender` is a registered token (`_revertIfSenderIsNotDepositToken`) — the victim has no say.

### Impact Explanation
Temporary freezing of position-critical functionality. A victim with an underwater-trending position cannot deposit a *new* collateral type to restore health; if all their existing slots are exhausted by attacker dust, they can only repay debt or deposit collateral they already hold. Front-running a victim's first-time deposit or first borrow makes that transaction revert. Recovery requires the victim to fully transfer out each dust token (up to 30 transactions, each of which must zero a balance the attacker can re-dust in the same block class of attack), so the DoS is temporary but renewable and can be timed to force a liquidation that would otherwise have been prevented. It also blocks an account from ever opening a position in newly added markets while dusted.

### Likelihood Explanation
Low-medium. The attack is permissionless and cheap (dust amounts of each listed collateral/synthetic plus gas), requires no privileged role, and works on the deployed configuration since `MAX_TOKENS_PER_USER` is a constant. However, impact is bounded: existing deposits/withdrawals and borrows of already-held tokens still work, and the victim can un-dust themselves, so it mainly serves as a timed griefing/liquidation-forcing tool rather than a permanent lock.

### Recommendation
- Do not couple "balance > 0" tracking to a hard revert on user-facing actions: on reaching the cap, either auto-skip adding to the enumerable set (track solvency off a minimum-balance threshold) or treat sub-dust balances as absent.
- Reject `deposit`/`transfer` amounts below a meaningful minimum (`minDepositAmount`), making 1-wei dust entries impossible.
- Alternatively, allow anyone to call `removeFromDepositTokensOfAccount`-style cleanup for zero/dust balances so recovery is permissionless.
- Consider checking the cap only on first-time deposit initiated by the account owner rather than on recipient-blind transfers.

### Proof of Concept
```ts
// Hardhat, mainnet fork. Victim has an open position; attacker is an EOA.
const MAX = await pool.MAX_TOKENS_PER_USER(); // 30
const victim = alice.address;

// 1) Attacker dust-deposits every listed collateral type to victim.
for (const dtAddr of await pool.getDepositTokens()) {
  const dt = await ethers.getContractAt('DepositToken', dtAddr);
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await underlying.connect(whale).transfer(attacker.address, 1);
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim); // adds dt to victim's set
}
// 2) Optionally dust synthetic DebtTokens the same way to reach 30 total.

// 3) Victim tries to deposit a collateral type they don't yet hold:
await expect(
  newDepositToken.connect(alice).deposit(amount, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4) Victim tries to issue a synthetic they don't yet hold:
await expect(
  pool.connect(alice).issue(newSynth.address, amount)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Key code: `Pool.sol:143-148` (`onlyIfAdditionWillNotReachMaxTokens`), `Pool.sol:204-220` (`addTo{Debt,Deposit}TokensOfAccount`), `DepositToken.sol:485-488` and `517-520` (uncontested additions to the recipient's set), `DepositToken.sol:459-462` (removal only on zero balance).