### Title
Missing recipient-consent check lets attacker fill a victim's per-account token list via dust DepositToken transfers, blocking deposits/receipts - (File: contracts/DepositToken.sol)

### Summary
Metronome tracks every `DepositToken`/`DebtToken` an account holds in `Pool.depositTokensOfAccount`/`debtTokensOfAccount` (`MappedEnumerableSet`) and caps it with `MAX_TOKENS_PER_USER`. Entries are added in `Pool.addToDepositTokensOfAccount`, which is invoked automatically from `DepositToken._transfer` whenever the recipient's balance goes 0 → >0. There is no check that the recipient authorized the addition. An unprivileged attacker can therefore write entries into a *victim's* account list by transferring dust amounts of each registered deposit token, exactly the "missing ownership check on writes to another user's state" class (CVE-2026-85669 analog).

### Finding Description
- `DepositToken.transfer`/`transferFrom` only verify the sender's unlocked balance via `_revertIfLocked` (DepositToken.sol:350,362). The recipient is arbitrary.
- On first receipt, `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` (DepositToken.sol:518-520).
- `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once `debtTokensOfAccount + depositTokensOfAccount >= MAX_TOKENS_PER_USER` (Pool.sol:143-148, 216-219).
- Consequently:
  - `DepositToken._mint` also calls `addToDepositTokensOfAccount` (DepositToken.sol:486-488), so `deposit(amount, victim)` for a *new* collateral type reverts once the victim's list is full.
  - Any `transfer`/`transferFrom`/`seize` to the victim of a token they don't already hold reverts, since `_transfer` hits the same cap check.
- `Operator.execute` can batch the whole attack: deposit dust into each listed `DepositToken`, then `transfer` 1 wei of each msdTOKEN to the victim.

### Impact Explanation
The attacker permanently pollutes the victim's account-list storage: the victim cannot deposit any collateral type not already in their list and cannot receive new deposit tokens (e.g., liquidation proceeds via `seize`, SmartFarmingManager flows, or transfers). This is a liveness/temporary-freezing violation — user funds are temporarily frozen because core operations revert. Mitigation exists (the victim can transfer each dust balance out so `removeFromDepositTokensOfAccount` fires at zero balance, DepositToken.sol:523-525), but only after noticing and spending gas per token; note attacker can re-fill slots cheaply, and `claimRewards`/onBehalf deposits from third parties also revert in the interim.

### Likelihood Explanation
- Fully permissionless: only public entry points (`DepositToken.deposit` + `transfer`, optionally via `Operator.execute`).
- Cost is dust collateral + deposit fee per listed deposit token; flash-loanable.
- No modifier stops it: `transfer` has no `nonReentrant` issue, no pause check on `transfer`, and the cap modifier enforces the limit but never authenticates that `account_` consented.
- Caveat/limitation: the victim can self-recover by zeroing each dust balance, and entries are removed at zero balance, so the freeze is temporary rather than permanent.

### Recommendation
Let `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` track a per-account opt-in or count only tokens the account explicitly enabled; alternatively allow removals for dust/zero-effect balances or cap counting to accounts with meaningful balances. At minimum, exempt `seize`/protocol-driven credits from the cap, or make the cap check tolerant so victim's own deposits are not blocked by unsolicited dust.

### Proof of Concept
Hardhat/Foundry fork sketch (Base/mainnet deployment):
```solidity
// victim has 1 deposit token in list; MAX_TOKENS_PER_USER = N
for (each DepositToken dt_i not held by victim) {
    underlying_i.approve(address(dt_i), dust);
    dt_i.deposit(dust, attacker);          // mints msdTOKEN to attacker
    dt_i.transfer(victim, 1);              // adds dt_i to victim's list (no consent)
}
// Now victim.depositTokensOfAccount + debtTokensOfAccount == MAX_TOKENS_PER_USER
// Any of these revert with UserReachedMaxTokens:
depositTokenNew.deposit(amount, victim);   // _mint -> addToDepositTokensOfAccount reverts
dt_new.transfer(victim, x);                // _transfer -> addToDepositTokensOfAccount reverts
```
Note: a full reproducible PoC (concrete `MAX_TOKENS_PER_USER` value and gas bounds) was not executed here; the value is defined in `Pool.sol`/`IPool` storage and the revert path is confirmed at Pool.sol:144-146 and DepositToken.sol:518-520.