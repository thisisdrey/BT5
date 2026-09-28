### Title
Attacker can exhaust a victim’s token slots with dust collateral, blocking new collateral positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` limits each account to 30 combined deposit/debt tokens. `DepositToken._transfer()` adds a token entry whenever a recipient’s balance changes from zero, and `Pool.addToDepositTokensOfAccount()` reverts once the account is already at `MAX_TOKENS_PER_USER`. An unprivileged attacker can therefore send dust balances of supported deposit tokens to a victim until all remaining slots are occupied. If the victim has debt and no unlocked balance, the victim cannot remove the dust entries or open a different collateral position until debt is repaid.

The deployed design intentionally caps the global number of deposit tokens at 30 because the fee collector itself receives deposit-token fees, so the attacker can realistically fill the entire available space in a sufficiently configured pool.

### Finding Description
`Pool.MAX_TOKENS_PER_USER` is fixed at 30, and `onlyIfAdditionWillNotReachMaxTokens()` reverts once an account already has 30 deposit/debt token entries. `DepositToken._transfer()` calls `pool.addToDepositTokensOfAccount(recipient_)` for a first-time nonzero recipient balance. No recipient opt-in or minimum amount is required.

An attacker can:

1. Deposit small amounts into supported collateral tokens.
2. Transfer one dust unit of each missing deposit token to the victim.
3. Fill the victim’s remaining account-list slots.
4. Leave the victim unable to receive or deposit a collateral type not already represented in the victim’s account list.

If the victim’s debt makes `unlockedBalanceOf()` return zero, the victim cannot transfer out or withdraw the dust balances to free slots. The victim can still deposit more of an already-held collateral type, and can recover by repaying debt, so this is not an unconditional permanent freeze.

### Impact Explanation
This can temporarily prevent a borrower from adding a new collateral type while underwater or fully utilized. That may prevent recovery actions that require a different collateral asset and can leave the account exposed to liquidation. Existing collateral withdrawals still follow the normal health checks, and already-held collateral remains usable, so this does not directly steal funds or permanently freeze all collateral.

### Likelihood Explanation
The attack requires a pool with enough registered deposit tokens to exhaust the victim’s remaining 30 slots and requires the victim to have sufficiently locked balances. The attacker pays the underlying needed to mint dust deposit-token balances plus transaction costs. Because the pool itself limits registered deposit tokens to 30, the bound is reachable when the victim does not already hold many token types.

### Recommendation
Allow an account to remove a dust position even when locked, or provide a public “forget token” path that transfers a negligible forced-received balance and removes it from the per-account list. Alternatives include tracking deposit-token eligibility separately from transferable balances, requiring a minimum economically meaningful transfer before adding a list entry, or only adding entries on deposit rather than ordinary transfer.

### Proof of Concept
A reproducible Hardhat test would:

1. Create one pool and register 30 deposit tokens.
2. Have the victim deposit collateral in one token and borrow enough that `unlockedBalanceOf(victim) == 0`.
3. Have the attacker deposit dust into every other `DepositToken` and call `transfer(victim, 1)` for each until `depositTokensOfAccount.length(victim)` reaches 30.
4. Attempt `DepositToken.deposit(amount, victim)` for a collateral type not currently in the victim’s list through a fresh account or replacement token scenario, or attempt a first-token transfer to the victim.
5. Observe `UserReachedMaxTokens` from `Pool.addToDepositTokensOfAccount`.
6. Show the dust cannot be removed while `unlockedBalanceOf(victim) == 0` because both `transfer()` and `withdraw()` call `_revertIfLocked`.

This demonstrates temporary blocking of new collateral positions, but it is a bounded liveness/griefing issue rather than unconditional theft or permanent loss.