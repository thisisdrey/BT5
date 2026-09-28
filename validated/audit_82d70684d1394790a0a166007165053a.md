### Title
Unauthenticated dust-transfer fills victim's per-account token list to `MAX_TOKENS_PER_USER`, permanently DoS-ing new collateral deposits and receipt of `msd` tokens - (File: contracts/Pool.sol)

### Summary
`Pool` enforces a hard cap of `MAX_TOKENS_PER_USER = 30` on the combined per-account lists of deposit tokens and debt tokens (`debtTokensOfAccount.length + depositTokensOfAccount.length`). Entries are added whenever an account's balance of a `DepositToken` goes from zero to non-zero — including via plain `transfer`/`transferFrom` and via `deposit(amount_, onBehalfOf_)`. An unprivileged attacker can deposit dust into every registered deposit token and send 1-wei transfers (or `deposit` on behalf of the victim) until the victim's list hits the cap. From then on, `addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens`, so the victim can never mint a new `DepositToken` type: `deposit()` of any collateral they don't already hold reverts, incoming `msd` token transfers revert, and `Pool.liquidate`'s `seize` to them reverts.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once the combined list length reaches 30: `contracts/Pool.sol:143-148`
- `addToDepositTokensOfAccount` is gated only by `_revertIfSenderIsNotDepositToken` — the *caller* must be a registered token, but the *target* `account_` is attacker-controlled: `contracts/Pool.sol:216-220`
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance was zero; no opt-in or minimum amount: `contracts/DepositToken.sol:517-520`
- `DepositToken._mint` (via `deposit`) does the same for `onBehalfOf_`: `contracts/DepositToken.sol:485-488`, `contracts/DepositToken.sol:211-237`
- Once at cap, any first-time mint/transfer to the victim reverts inside the token hook, so the entire `deposit`/`transfer`/`seize` transaction reverts. There is no way for the victim to remove entries other than fully emptying each dust balance (`removeFromDepositTokensOfAccount` only fires on `balanceOf == 0`: `contracts/DepositToken.sol:522-525`), and the attacker can re-fill freed slots in the same block since `transfer` is unauthenticated.

Bug-class analog: the Mosquitto issue is unauthenticated resource exhaustion via cheap inbound operations. Here, cheap unauthenticated `transfer`/`deposit` calls exhaust a bounded per-account resource (the 30-slot token list), yielding a protocol-level availability failure.

### Impact Explanation
- A victim who holds debt and needs to deposit a *new* collateral type to restore health cannot do so — deposits revert, so the position drifts to liquidation while remediation is blocked.
- Any protocol counterparty (OTC buyer, router, liquidator receiving seized `msd` tokens if it ever goes to zero balance) cannot receive new `msd` tokens.
- The attack is repeatable and cheap: it costs only dust amounts of each registered underlying plus gas, and re-griefing after the victim clears a slot is trivial.
- Invariant broken: liveness of deposit/transfer/liquidation paths — a temporary freezing of users' ability to move or add funds.

### Likelihood Explanation
- Requires no privileges, no oracle manipulation, no governance action — only public entry points (`DepositToken.deposit`, `DepositToken.transfer`).
- Feasibility depends on the pool having enough registered deposit tokens for dust transfers to matter: `addDepositToken` itself is capped at `MAX_TOKENS_PER_USER` (`contracts/Pool.sol:703`), so a pool with 30 registered collaterals lets an attacker fill the entire list alone; with fewer registered tokens the attacker can still consume all remaining slots and block the victim's next new collateral.
- Cost scales linearly with the number of tokens; no flash loan needed.

### Recommendation
- Do not add entries on plain `transfer`/first-time receipt in `DepositToken._transfer` (only on `deposit`/mint and `seize`), or
- Whitelist-free cleanup: allow `addToDepositTokensOfAccount` to evict/skip rather than revert, or
- Bound dust: require a minimum balance / only track tokens above a threshold in `debtPositionOf`, or
- Let users remove their own list entries directly (`removeMyDepositToken`) so griefing is self-healing without full-emptying each balance.

### Proof of Concept
Hardhat sketch against deployed configuration:

```ts
// setup: `pool`, registered DepositTokens dt[0..N-1] with underlyings u[i]
// victim holds some debt position or simply an account we want to DoS
const attacker = await ethers.getSigner(0);
const victim = '0xVictim...';

const max = (await pool.MAX_TOKENS_PER_USER()).toNumber();
const existing = (await pool.getDepositTokensOfAccount(victim)).length
               + (await pool.getDebtTokensOfAccount(victim)).length;

for (let i = 0; i < max - existing; ++i) {
  const dt = depositTokens[i % depositTokens.length];
  const u = await dt.underlying();
  // acquire dust of underlying, then deposit on behalf of victim
  await IERC20(u).connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim); // adds dt to victim's list on first receipt
}

expect((await pool.getDepositTokensOfAccount(victim)).length
     + (await pool.getDebtTokensOfAccount(victim)).length).to.eq(max);

// victim is now DoS-ed: any deposit into a collateral they don't already hold reverts
const newDt = depositTokens.find(t => !heldBy(victim, t));
await expect(
  newDt.connect(victimSigner).deposit(parseUnits('100', await newDt.decimals()), victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// same for inbound transfers / liquidations seizing to a fresh account
await expect(
  depositTokens[0].connect(other).transfer(victim, 1) // if dt[0] not already in list -> reverts
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Run on a fork of Base/Mainnet where the deployed pool has a high count of registered deposit tokens (cap is 30, per `addDepositToken`), demonstrating the revert path end-to-end with real `SynthContext` sender resolution.