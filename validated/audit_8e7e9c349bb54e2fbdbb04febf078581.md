### Title
Unprivileged attacker can fill a victim's token list via dust `DepositToken` transfers, blocking all new collateral deposits and transfers to the victim - (File: contracts/Pool.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across a combined per-account set of deposit tokens and debt tokens via `onlyIfAdditionWillNotReachMaxTokens`, reverting with `UserReachedMaxTokens` once `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30`. Any ERC20 `DepositToken` balance (even 1 wei) causes `DepositToken._transfer` to call `pool.addToDepositTokensOfAccount(recipient)`, which is guarded by that modifier. Since `DepositToken.transfer` is a public, unauthenticated entry point and there is no opt-in or minimum amount, an attacker can deposit dust into every registered deposit token and then `transfer` a single wei of each token to any victim address, forcibly occupying up to 30 slots in the victim's `depositTokensOfAccount` set. Once the victim's combined list reaches the cap, every subsequent `deposit(onBehalfOf = victim)` for a token the victim does not already hold, every `transfer`/`transferFrom` of a new deposit token to the victim, every `seize` crediting a new token, and every SmartFarmingManager deposit on their behalf reverts.

### Finding Description
The analog to the MySQL optimizer availability bug (repeatable crash via a reachable call path) is this forced-slot-filling DoS:

- `Pool.sol` (lines 79, 143-148): `MAX_TOKENS_PER_USER = 30`; `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` when the sum of both lists is at the cap.
- `DepositToken.sol::_transfer` (lines 498-526): on any successful transfer where `_recipientBalanceBefore == 0 && amount_ > 0`, it calls `pool.addToDepositTokensOfAccount(recipient_)` — there is no minimum amount and no recipient consent.
- `DepositToken.sol::transfer` (lines 348-354) is callable by any EOA holding unlocked balance; `seize` (lines 343-345) and `_mint` (lines 469-489) follow the same add-on-first-balance logic.
- `Pool.addDepositToken` caps the pool at 30 deposit tokens total (line 703), so on a fully populated pool an attacker can fill essentially the entire allowance of a victim who holds few or no positions.

Attack steps:
1. Attacker deposits a tiny amount of collateral into each of the pool's N deposit tokens (`deposit(amount, attacker)`), receiving `msdX` balances.
2. For each deposit token `T_i`, attacker calls `T_i.transfer(victim, 1)`. Each call pushes `T_i` into `depositTokensOfAccount[victim]` at ~zero cost (transfers have no fee; `unlockedBalanceOf` allows it since the attacker keeps debt at 0).
3. Once `debtTokensOfAccount[victim] + depositTokensOfAccount[victim] >= 30`, the victim is locked out of any collateral type they do not already hold.

The victim cannot prevent this — there is no way to reject incoming `DepositToken` transfers — and cannot free a slot without fully zeroing a token balance (transfer out or `withdraw` of the whole dust amount).

### Impact Explanation
Temporary freezing of funds / availability denial:

- The victim cannot deposit any new collateral type (`deposit` → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`). For a leveraged position approaching liquidation whose rescue requires adding a *different* collateral, every recovery transaction reverts, making forced liquidation loss practical to engineer.
- The victim cannot receive any new deposit token via `transfer`, `transferFrom`, `seize`, or SmartFarmingManager flows — protocol-level liveness denial for that account.
- The attacker can repeatedly re-dust every slot the victim empties, extending the freeze indefinitely for the cost of gas only.

### Likelihood Explanation
Fully reachable by an unprivileged EOA: `deposit` and `transfer` are public, `whenNotPaused` only, require no role, no oracle manipulation, and no privileged actor. Cost is bounded by dust collateral (~30 wei-class amounts) plus gas. It is blocked only when the pool has few deposit tokens registered or the victim already holds near the cap in a way that favors them; on the deployed configuration with many deposit tokens the attack is cheap and repeatable.

### Recommendation
Exclude zero-effect dust additions or decouple the cap from transfers:
- Only add a token to `depositTokensOfAccount` when the credit exceeds a minimum threshold, or
- Perform the `UserReachedMaxTokens` check in `deposit`/`mint` paths but let plain `transfer`/`seize` skip the set registration (or let transfers to an already-full account proceed without registering), or
- Track set membership lazily in `debtPositionOf`/`depositOf` iteration instead of on every balance change.

### Proof of Concept
Hardhat-style reproduction (forkeable against deployed pool tokens):

```ts
// Setup: pool with registered deposit tokens T[0..n], attacker has deposited
// a minimal amount in each so balanceOf[attacker] > 0 and unlocked.

const victim = target.address;
const tokens = await pool.getDepositTokens(); // up to 30

for (const t of tokens) {
  const dt = await ethers.getContractAt('DepositToken', t);
  await dt.connect(attacker).transfer(victim, 1);   // adds slot
}

// victim's list is now full (or full combined with debtTokensOfAccount)
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(tokens.length);

// Victim tries to deposit a collateral type they don't yet hold:
const newDeposit = await ethers.getContractAt('DepositToken', tokens[0] /* any token victim's balance is 0 in */);
await newDeposit.connect(victim).approve?.(0); // not needed; deposit is by underlying
await underlying.connect(victim).approve(newDeposit.address, amount);
await expect(
  newDeposit.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Any incoming transfer of a token the victim doesn't hold also reverts:
await expect(
  someOtherDepositToken.connect(attacker).transfer(victim, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Expected outcome: all calls that would credit a *new* token to the victim revert with `UserReachedMaxTokens`, while the victim retains only withdraw/transfer-out ability — matching the availability-loss bug class of CVE-2019-2581 (repeatable denial of service through a reachable, low-privilege path).

Uncertainty note: I verified the modifier, the set-add calls in `_transfer`/`_mint`, and the 30-token cap directly in `contracts/Pool.sol` and `contracts/DepositToken.sol`. I could not confirm in remaining iterations whether `debtPositionOf`/`depositOf` iterate these lists in a way that makes the filled set additionally harmful (e.g., reverting views), but the deposit/transfer block above is sufficient for the temporary-freeze impact.