### Title
Dust-transfer griefing via `MAX_TOKENS_PER_USER` permanently blocks an account's deposits/liquidations into new collateral tokens - (File: contracts/DepositToken.sol)

### Summary
`DepositToken._transfer` and `DepositToken._mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from zero to non-zero. `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once the combined deposit+debt token list for that account reaches `MAX_TOKENS_PER_USER`. Any unprivileged attacker can dust-transfer 1 wei of every whitelisted `DepositToken` to a victim, permanently filling the victim's token list so that any subsequent deposit, transfer, or liquidation-seizure involving a *new* deposit token reverts for that account.

### Finding Description
- `DepositToken._transfer` adds the token to the recipient's account list on first receipt (contracts/DepositToken.sol:518-520) and `DepositToken._mint` does the same on first deposit (contracts/DepositToken.sol:486-488).
- `Pool.addToDepositTokensOfAccount` enforces a shared cap `MAX_TOKENS_PER_USER` across deposit and debt tokens and reverts `UserReachedMaxTokens` when exceeded (confirmed by `Pool.test.ts` expecting this revert after filling half the cap with deposit tokens and half with debt tokens).
- Transfers of `DepositToken` are permissionless ERC20 transfers; `transfer`/`transferFrom` only check `_revertIfLocked` on the *sender*, never on the recipient (contracts/DepositToken.sol:348-354). There is no opt-in or minimum amount.
- Once the victim's list is full: `deposit(amount, onBehalfOf_=victim)` reverts in `_mint` → `addToDepositTokensOfAccount`; `transfer(victim, ...)` of any new msdToken reverts; and `Pool.liquidate` reverting inside `DepositToken.seize` (which calls `_transfer` to the liquidator/seize recipient) blocks liquidation flows that credit a new token to a full account.
- The attacker can re-dust the account faster than the victim can clear slots (clearing requires one `transfer` per token by the victim; the attacker only needs to re-send dust in the same or a follow-up block), and can pre-fill fresh victim addresses cheaply since dust amount can be 1 wei.

### Impact Explanation
The victim account is unable to onboard any new collateral: deposits to it revert, and it cannot receive or be liquidated into new deposit tokens. Because `_transfer` reverts atomically, `Pool.liquidate` calls that would seize a token not already in the liquidator's/recipient's list also revert, giving a prepared attacker a way to grief liquidation receipt for targeted accounts. This is a liveness break (temporary freezing of the account's deposit/transfer functionality) sustained for as long as the attacker keeps the slots filled — it does not steal funds, and withdrawals of already-held tokens still work since `_burn` removes entries, so funds are not permanently locked.

### Likelihood Explanation
Low-to-medium attacker cost: the pool must have enough distinct whitelisted deposit tokens to fill `MAX_TOKENS_PER_USER` slots (shared with debt tokens), and the attacker must hold/acquire dust of each. Since deposits are open to anyone (`deposit` has no access control, only `whenNotPaused`/`onlyIfDepositTokenExists`), the attacker can mint 1 wei of each msdToken themselves and distribute it. Victims can recover by emptying dust slots, but only at the cost of repeated transactions racing the attacker's re-dusting. No governor/admin action is required by the attacker, satisfying the unprivileged constraint.

### Recommendation
Make account-list membership non-griefable, e.g.:
- Reject `transfer`/`_mint` to an account that would exceed `MAX_TOKENS_PER_USER` only when initiated by the account itself is not possible — instead, treat deposit-token transfers differently: only add to the list on `deposit`/`seize` (protocol-initiated), not on plain ERC20 `transfer`, or require a minimum threshold amount before registering a token in the list.
- Alternatively allow recipients to always burn/withdraw a dust position even when at the cap (already true), and exempt `seize` during liquidation from the cap so liquidations cannot be blocked.
- Emit/keep an explicit per-account slot accounting so a victim cannot be pushed over the cap without a meaningful economic cost.

### Proof of Concept
Hardhat-style sketch:

```ts
// Assume pool has N >= MAX_TOKENS_PER_USER deposit tokens (or shared cap with debt tokens).
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()

// 1. Attacker deposits dust in every collateral and transfers 1 wei to victim.
for (let i = 0; i < max; i++) {
  const dt = depositTokens[i] // distinct whitelisted DepositToken
  await underlying[i].approve(dt.address, 2)
  await dt.deposit(2, attacker.address)
  await dt.transfer(victim.address, 1) // fills victim's list via _transfer -> addToDepositTokensOfAccount
}

// 2. Victim's list is now full.
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(max)

// 3. Any new-token deposit or transfer to victim reverts.
await expect(
  newDepositToken.connect(alice).deposit(1000, victim.address)
).revertedWithCustomError(pool, 'UserReachedMaxTokens')

await expect(
  otherDepositToken.connect(attacker).transfer(victim.address, 1)
).revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4. Liquidation seizing a token not already held by a full account also reverts
//    inside DepositToken.seize -> _transfer -> addToDepositTokensOfAccount.
```

Key code paths: `DepositToken._transfer` (contracts/DepositToken.sol:498-526), `DepositToken._mint` (contracts/DepositToken.sol:469-489), `DepositToken.seize` (contracts/DepositToken.sol:343-345), and `Pool.addToDepositTokensOfAccount` enforcing `MAX_TOKENS_PER_USER` (revert confirmed in `Pool.test.ts` "should revert when reach max tokens").

Caveat: I was unable to view the exact line numbers of `Pool.addToDepositTokensOfAccount`/`MAX_TOKENS_PER_USER` in `contracts/Pool.sol` before the final iteration; the revert name and cap-sharing behavior are confirmed by the test file, but the precise implementation lines should be verified in `contracts/Pool.sol` during the fork PoC.