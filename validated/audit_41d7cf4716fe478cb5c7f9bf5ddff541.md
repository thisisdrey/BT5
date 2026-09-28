### Title
Unauthenticated attacker can fill a victim's per-account token list with dust, permanently blocking new collateral deposits and debt issuance while the position has locked balance - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` enforce `MAX_TOKENS_PER_USER = 30` across the combined deposit-token and debt-token lists and revert with `UserReachedMaxTokens` once the cap is hit. Because `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint dust `msdTOKEN` to an arbitrary `onBehalfOf_` victim, an unprivileged attacker can cheaply fill all 30 slots of any target account. Every subsequent first-time mint or transfer of a deposit token to that victim reverts inside `_mint`/`_transfer` via `pool.addToDepositTokensOfAccount(recipient_)`. If the victim has an open debt position, their deposit-token balances are locked by `_revertIfLocked`/`unlockedBalanceOf`, so the victim cannot remove the attacker's dust entries to free slots — the block persists for as long as the debt exists.

### Finding Description
- `Pool.sol:143-148` — `onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= 30`.
- `Pool.sol:204-220` — `addToDebtTokensOfAccount`/`addToDepositTokensOfAccount` are callable only by registered tokens, but they are triggered on behalf of any account.
- `DepositToken.sol:211-237` — `deposit(amount_, onBehalfOf_)` is permissionless; `onBehalfOf_` is attacker-chosen.
- `DepositToken.sol:486-488` — `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance goes from 0 to >0; the revert propagates to the caller.
- `DepositToken.sol:518-520` — `_transfer` does the same, so even plain ERC20 transfers of a new deposit token to a capped victim revert.
- `DepositToken.sol:180-182, 383-398` — a victim with debt has `unlockedBalanceOf` constrained by `_issuableInUsd`; dust positions cannot be transferred/withdrawn to clear slots once the position is unhealthy.

Attack sequence (all unprivileged EOA calls):
1. Victim has any open debt position (or attacker waits for one).
2. For each enabled `DepositToken` (and via `DebtToken.issue(amount_, victim)` for debt tokens), attacker calls `deposit(1 wei, victim)` — each call pushes a new entry into the victim's `depositTokensOfAccount`.
3. Once `debtTokens + depositTokens == 30`, any victim action that would add a new token type reverts: depositing a different collateral, receiving `msdTOKEN` transfers, issuing a new synthetic.

### Impact Explanation
Temporary freezing of funds / forced liquidation: a victim holding a single collateral type whose `issuableInUsd` drops to 0 (price decline) cannot deposit a *different* collateral type to restore health — the `addToDepositTokensOfAccount` revert bricks the rescue path, and the locked dust entries cannot be removed. The victim's position becomes liquidatable with no self-recourse on new collateral types. Additionally, any incoming transfer of a new deposit token to the victim permanently reverts while capped.

### Likelihood Explanation
Cost is ~30 × (dust deposit + gas); all calls are public and unprivileged on the deployed configuration (`deposit` has no access control, `onlyIfAdditionWillNotReachMaxTokens` is the only gate). The attack is cheaper the fewer token types the victim already holds, and can be front-run repeatedly.

### Recommendation
Track per-account token lists only for entries the account itself opted into, or allow slot eviction: e.g., skip the cap check for accounts that already hold the token, let `removeFromDepositTokensOfAccount` be triggered permissionlessly for dust balances, or make `deposit`'s beneficiary opt-in. Alternatively, exclude `unlockedBalanceOf`-locked dust from blocking removal.

### Proof of Concept
Hardhat fork test: deploy Pool + two DepositTokens (as in `test/Pool.test.ts:1386-1416`), have attacker call `msdX.deposit(1, victim)` across all registered deposit tokens until `getDepositTokensOfAccount(victim).length == 30`; then `msdNew.deposit(amount, victim)` and `msdNew.transfer(victim, 1)` both revert with `UserReachedMaxTokens`. With victim holding debt (`unlockedBalanceOf(victim) == 0`), `transfer`/`withdraw` of the dust entries reverts with `NotEnoughFreeBalance`, confirming persistence.