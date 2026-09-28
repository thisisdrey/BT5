### Title
Attacker can fill a victim's per-account token list with dust `DepositToken` transfers, blocking the victim from depositing new collateral types or taking on new debt positions - (File: contracts/Pool.sol)

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` across the union of `debtTokensOfAccount` and `depositTokensOfAccount` (`Pool.sol:79`, `Pool.sol:143-148`). New entries are appended by `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount`, which are callable only by registered `DepositToken`/`DebtToken` contracts (`Pool.sol:204-220`) — these are invoked from the tokens' balance-change hooks whenever an account's balance moves from 0 to non-zero. Because `DepositToken` is a freely transferable ERC20, an attacker can push dust amounts of many different deposit tokens to a victim, filling the victim's list. Once full, any action that would add a new token to the victim's set reverts with `UserReachedMaxTokens`.

### Finding Description
The bug class in the external report — a bounded token-address list that an unprivileged actor can saturate to grief a victim — maps directly onto Metronome's per-account token accounting:

- `Pool.sol:79`: `uint256 public constant MAX_TOKENS_PER_USER = 30`
- `Pool.sol:143-148`: `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`
- `Pool.sol:216-220`: `addToDepositTokensOfAccount` is invoked by `DepositToken` contracts on 0→nonzero balance transitions; the only check on the *recipient* is the cap

Attack path (all unprivileged):
1. Attacker deposits minimal amounts of underlying into every active `DepositToken` in the pool (or receives them via `transfer`), acquiring dust balances of each deposit token.
2. Attacker calls `depositToken.transfer(victim, 1)` for each distinct deposit token. Each transfer moves the victim's balance for that token from 0→nonzero, so `Pool.addToDepositTokensOfAccount(victim)` appends the token to the victim's set. No opt-in or allowance is needed — ERC20 transfers are permissionless to any recipient.
3. After ~30 transfers (fewer needed if the victim already holds positions, since debt and deposit tokens share the same cap), `debtTokensOfAccount.length + depositTokensOfAccount.length == 30`.
4. Now every subsequent operation that would register a *new* token for the victim reverts with `UserReachedMaxTokens`:
   - `DepositToken.deposit(..., onBehalfOf = victim)` for any collateral type the victim does not already hold (the mint triggers the add, which reverts, reverting the whole deposit).
   - `DebtToken.issue`/`mint` of a synthetic asset whose debt token is not yet in the victim's set.
   - Receipt of any additional deposit-token type via `transfer` or `seize`.

The invariant that breaks is liveness/availability of core protocol functions for a targeted account, driven entirely by a third party — the same griefing shape as the OpenQ finding (victim's ability to use a non-pre-registered token is denied by an attacker consuming the bounded slots).

### Impact Explanation
- A victim with an unhealthy position who needs to deposit a **new** collateral type to restore health cannot do so; their position can be liquidated while the rescue path is blocked — indirect loss of funds via forced liquidation.
- A victim cannot open debt in a new synthetic asset or accept deposit-token transfers they may legitimately expect.
- Recovery requires the victim to spend gas withdrawing/transferring out each dust token (which calls `removeFromDepositTokensOfAccount` on 0-balance) — one transaction per slot, so up to 30 recovery transactions. Until then the functionality is frozen. This is a temporary freezing of protocol functionality plus a forced-liquidation vector, matching "temporary freezing of funds" / griefing acceptance criteria.
- Note: existing positions are not frozen — withdrawals, repayments, and deposits of collateral types already in the victim's set still work, because `set.add` is only invoked on 0→nonzero transitions.

### Likelihood Explanation
- Cost is real but modest: attacker must supply actual underlying collateral to each `DepositToken` (or acquire the deposit tokens), and pays one transfer per slot. With many collateral types registered, 30 slots are reachable.
- No privileged role, oracle manipulation, or timing requirement is needed; transfers can be executed atomically or front-run ahead of the victim's deposit.
- The mechanism is an explicit, tested protocol invariant (`test/Pool.test.ts:1386-1416` tests `UserReachedMaxTokens`), so it is guaranteed to trigger once the cap is reached — the only uncertainty is per-pool count of registered deposit tokens.

### Recommendation
- Exempt the token-add cap check when the balance increase comes from a deposit/mint initiated by the account owner (i.e., enforce the cap on unsolicited `transfer`/`seize` paths only), or
- Allow `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` to no-op (rather than revert) when the cap is reached and degrade gracefully in `depositOf`/`debtOf` accounting, or
- Make the transfer hook pull-based: only register tokens for accounts that opt in, so dust cannot consume slots.

### Proof of Concept
Hardhat sketch (pool, depositTokenA…Z = registered `DepositToken`s, attacker, victim):

```ts
// attacker holds dust of each deposit token (deposited minimal underlying)
const victim = alice.address;
const tokens = [depositTokenA, /* ... one per registered collateral ... */];

for (const dt of tokens) {
  await dt.connect(attacker).transfer(victim, 1); // each 0->nonzero adds to victim's set
}
// pad remaining slots with further deposit tokens until:
expect(
  (await pool.getDepositTokensOfAccount(victim)).length +
  (await pool.getDebtTokensOfAccount(victim)).length
).to.eq(await pool.MAX_TOKENS_PER_USER()); // 30

// victim tries to deposit a collateral type they don't yet hold
await underlying.connect(alice).approve(depositTokenNew.address, amount);
await expect(
  depositTokenNew.connect(alice).deposit(amount, alice.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim cannot be rescued with a new collateral type near liquidation;
// recovery requires ~30 withdraw/transfer-out txs to empty dust slots
```

Unverified detail: I confirmed the cap, the modifier, and the token→pool registration functions in `contracts/Pool.sol`, and that `DepositToken.sol`/`DebtToken.sol` contain the `pool.addTo*` calls, but I did not read the exact transfer/mint hook lines in `DepositToken.sol` (read truncated at line 120 and grep output was count-only). The doc comments at `Pool.sol:200-215` state these are called from the tokens "when user's balance changes from 0", which is the standard `_afterTokenTransfer`/`_update` hook pattern the PoC relies on.