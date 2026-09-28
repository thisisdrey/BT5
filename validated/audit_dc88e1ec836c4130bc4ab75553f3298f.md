### Title
Unprivileged attacker can insert entries into a victim's per-account token lists and permanently/temporarily block their deposits and debt issuance via `MAX_TOKENS_PER_USER` griefing — (File: contracts/DepositToken.sol, contracts/Pool.sol)

### Summary
The MySQL CVE describes a low-privileged user making unauthorized insert/update operations on data. The analog in Metronome is unauthorized insertion into another account's `depositTokensOfAccount` enumerable set: any EOA can force entries into a victim's per-account token list via `DepositToken.deposit(amount_, onBehalfOf_)` or dust `transfer()`. Because `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` across both deposit and debt token lists, an attacker can fill a victim's list and cause all future first-time deposits and first-time debt mints for that victim to revert with `UserReachedMaxTokens`, i.e., an unauthorized write to the victim's accounting state that denies service.

### Finding Description
`DepositToken.deposit(uint256 amount_, address onBehalfOf_)` mints deposit tokens to an arbitrary `onBehalfOf_` address (contracts/DepositToken.sol:211-237). `_mint` adds the token to `onBehalfOf_`'s account list whenever the recipient's prior balance is zero (contracts/DepositToken.sol:486-488). The same insertion happens in `_transfer` for dust transfers (contracts/DepositToken.sol:517-520). Both paths call `Pool.addToDepositTokensOfAccount`, which reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30` (contracts/Pool.sol:143-148, 216-220). Symmetrically, `DebtToken._mint` inserts into `debtTokensOfAccount` (contracts/DebtToken.sol:597-600), so a filled list also blocks the victim from issuing new synth types.

There is no consent check: the victim never opted in to receiving the deposit tokens, yet an entry is written into their accounting set — the "unauthorized insert" of the bug class. With enough registered deposit tokens (the pool supports up to 30 deposit tokens per `addDepositToken`, contracts/Pool.sol:703), an attacker can occupy all 30 slots of a target account.

### Impact Explanation
- The victim cannot deposit into any deposit token they don't already hold (every `_mint` to a zero-balance token reverts), and cannot issue any new debt token type (`_mint` in `DebtToken` reverts the same way). This is a persistent denial of core protocol functionality for the victim.
- Removal requires the victim to zero out each dust balance. If the victim has outstanding debt, dust balances can be locked (`unlockedBalanceOf` returns less than balance when `_debtInUsd > 0`, contracts/DepositToken.sol:383-398), making the dust entries non-removable without repaying debt or adding more collateral — temporary freezing of position-management capability.
- If the victim has no debt, they can transfer/withdraw dust to free slots, but the attacker can refill them, forcing an ongoing griefing cost asymmetry.

### Likelihood Explanation
Attack cost is only the sum of dust deposits across registered deposit tokens and gas — cheap on L2 deployments (Base, Optimism, Hemi, Swell, BSC). No privileged role, oracle manipulation, or timing is needed. The limiting factor is the number of registered deposit/debt tokens in a given pool; the attack is most effective in pools with many registered tokens.

### Recommendation
Only add a deposit token to an account's list when the account is the payer (`onBehalfOf_ == _msgSender()` or via an explicit opt-in), or move the list-add out of `_transfer`/`_mint` for unsolicited recipients. Alternatively, allow anyone to purge dust entries below a threshold from `depositTokensOfAccount`, or let a user forcibly burn zero-interest dust balances regardless of lock status so slots can always be reclaimed.

### Proof of Concept
Hardhat/fork sketch:

```ts
// Pool has >=2 registered deposit tokens (e.g., msdMET, msdWBTC) and victim has some position.
const victim = alice.address

// Attacker deposits 1 wei of underlying on behalf of the victim for each deposit token
// the victim does not yet hold. Each call inserts the token into victim's list:
// DepositToken._mint -> pool.addToDepositTokensOfAccount(victim)
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying())
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, victim) // writes entry into victim's set
}

// Repeat until debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) == 30
expect(await pool.getDepositTokensOfAccount(victim)).to.have.lengthOf(30)

// Victim's next deposit into a token they don't hold reverts:
await underlying2.connect(victim).approve(newDepositToken.address, amount)
await expect(
  newDepositToken.connect(victim).deposit(amount, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

If the victim carries debt and the dust is locked (`unlockedBalanceOf(victim) < dust`), the slots cannot be cleared by `transfer`/`withdraw`, so the DoS persists until the victim repays debt.