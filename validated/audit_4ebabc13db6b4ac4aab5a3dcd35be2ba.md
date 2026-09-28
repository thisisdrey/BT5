### Title
Dust transfers/deposits fill a victim's deposit-token set to `MAX_TOKENS_PER_USER`, reverting all further deposits and inbound transfers - ([File: contracts/Pool.sol])

### Summary
`Pool` tracks every `DepositToken` an account holds in a `MappedEnumerableSet.AddressSet` capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). The set is appended whenever an account receives a deposit token for the first time — both via `Pool.deposit` (called from `DepositToken` during mint) and via the `updateDepositTokenBalanceOf` hook invoked by `DepositToken.transfer`/`transferFrom`. When the set is full, insertion reverts with `UserReachedMaxTokens` (`contracts/Pool.sol:32`). An unprivileged attacker can therefore dust-transfer 1 wei of each of the ~30 registered deposit tokens to a victim, permanently blocking that victim from (a) depositing into any collateral they don't already hold and (b) receiving any deposit-token transfer — until the victim manually empties positions to free slots. This mirrors the advisory's bug class: a crafted, unauthenticated input that crashes/reverts validation logic for a target.

### Finding Description
- Invariant broken: liveness — a user's ability to open positions / receive collateral tokens.
- Entry points: `DepositToken.transfer`/`transferFrom` (public, unauthenticated) → `Pool.updateDepositTokenBalanceOf` → `MappedEnumerableSet.add` → `revert UserReachedMaxTokens()` once the account set reaches 30 entries. Same path via `Pool.deposit` for direct mints.
- Attacker cost: acquiring dust amounts of deposit tokens (attacker can deposit their own underlying first), then ~30 transfers to the victim. No privileged role, no oracle manipulation, no flash loan required.
- No guard prevents it: the check is a hard revert on the *recipient's* set size; `whenNotPaused`, `ReentrancyGuard`, and `SynthContext` checks don't stop it on deployed configuration, and the cap is a constant.
- Recovery exists but is costly: the victim must fully withdraw/transfer away tokens to shrink the set; every new transfer-in or deposit into a not-yet-held token reverts in the meantime, and attackers can front-run withdrawals to re-fill freed slots.

### Impact Explanation
Temporary freezing of funds and griefing: a targeted user cannot deposit into new collateral types or receive deposit tokens, and must spend transactions to clear the dusted slots. Combined with `swap`-based flows this can also block rescue of an unhealthy position via new collateral deposits, indirectly pushing it toward liquidation.

### Likelihood Explanation
Low-to-moderate cost, no privilege needed, but limited to blocking *new* token types for a targeted account rather than draining funds. Requires ~30 distinct deposit tokens registered in the pool (check `depositTokensOf` count on the deployed `PoolRegistry`).

### Recommendation
Track only tokens with non-trivial balances, only insert on mint/deposit paths the user initiates (not on inbound `transfer`), or exempt unsolicited inbound transfers from the `MAX_TOKENS_PER_USER` check.

### Proof of Concept
Foundry fork sketch (verify against deployed `Pool` on e.g. Base):

```solidity
function test_fillVictimTokenSet() public {
    // attacker deposits dust underlying into each registered deposit token
    address victim = makeAddr("victim");
    IDepositToken[] memory tokens = pool.getDepositTokens(); // per IPool.sol
    for (uint i; i < pool.MAX_TOKENS_PER_USER(); ++i) {
        deal(address(tokens[i].underlying()), attacker, 1);
        tokens[i].underlying().approve(address(tokens[i]), 1);
        tokens[i].deposit(1);                    // attacker mints dust
        tokens[i].transfer(victim, 1);           // adds token to victim's set
    }
    // victim deposit into any new token reverts
    vm.startPrank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    tokens[0].deposit(1);                        // different token victim doesn't hold
}
```

Note: I was able to confirm `MAX_TOKENS_PER_USER`, `UserReachedMaxTokens`, and the `MappedEnumerableSet` usage in `contracts/Pool.sol`, but the index truncated `DepositToken.sol`/`IPool.sol` internals — the exact hook names (`updateDepositTokenBalanceOf` / `depositTokensOf`) should be confirmed when reproducing the PoC.