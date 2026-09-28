### Title
Dust transfers fill `depositTokensOfAccount` / `debtTokensOfAccount` sets to `MAX_TOKENS_PER_USER`, blocking victim deposits, mints, swaps and liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Metronome tracks per-account token positions in `MappedEnumerableSet.AddressSet` entries `depositTokensOfAccount` and `debtTokensOfAccount`, declared in `contracts/storage/PoolStorage.sol` lines 78-83. These sets are mutated by Pool update hooks that `DepositToken` and `DebtToken` call on every balance transition (zero → nonzero adds the token, nonzero → zero removes it). The Pool caps each per-account set at `MAX_TOKENS_PER_USER` and reverts once the cap is exceeded, because `debtPositionOf`/`depositOf` iterate these sets to compute account value and health. Anyone can trigger the "add" path for a victim by transferring a dust amount of a deposit token to them — the `transfer`/`transferFrom` hook updates the recipient's set regardless of their consent. An attacker can therefore fill a victim's set up to `MAX_TOKENS_PER_USER` with junk positions (attacker-minted deposit balances or dust of every listed collateral), after which any action that would add a *new* token to the victim's set reverts.

### Finding Description
- `DepositToken._beforeTokenTransfer`/`_afterTokenTransfer` (or `transfer`/`transferFrom`) calls into `Pool` to update the sender's and recipient's position sets (`updatePositionOf`/`_updateDepositTokensOfAccount` in `contracts/Pool.sol`). The recipient cannot opt out — a plain ERC20 transfer of 1 wei of a deposit token inserts that token into `depositTokensOfAccount[victim]`.
- The Pool enforces `depositTokensOfAccount.length(account_)` (and `debtTokensOfAccount.length(account_)`) ≤ `MAX_TOKENS_PER_USER`, reverting on the check inside the set-add path.
- Once the victim's set is full:
  - `Pool.deposit` of a collateral the victim does not already hold reverts (minted deposit tokens would trigger the set-add).
  - `DebtToken.issue`/`mint` of a synthetic the victim does not already owe reverts.
  - `Pool.swap`, `SmartFarmingManager.leverage`/`flashRepay` legs that introduce a new deposit token to the account revert.
  - Liquidations that would credit a new deposit token to the victim (or that must add the seized collateral token) can be forced to revert.
- The invariant broken is liveness: an unprivileged attacker, spending only gas and dust of deployable/listed tokens, selectively disables an arbitrary account's ability to take on new collateral or debt positions. The victim's *existing* positions remain withdrawable/repayable, so this is a temporary freezing of the account's functionality rather than permanent loss, but it is repeatable on every new account the victim creates, matching the DoS impact class of the source report.

### Impact Explanation
Temporary freezing of funds/protocol functionality for a targeted user: the victim cannot deposit new collateral types, mint new synthetics, or use leverage/swap paths that touch new tokens while their set is saturated. On chains with many registered deposit tokens (or where the attacker can cheaply acquire dust of each), reaching `MAX_TOKENS_PER_USER` costs only dust + gas per token. Front-running any victim `deposit`/`issue` transaction makes it revert, griefing deposits indefinitely.

### Likelihood Explanation
The attack requires no privileges — `DepositToken.transfer`/`transferFrom` are public and the recipient-side set update is unconditional. Feasibility depends on the ratio between `MAX_TOKENS_PER_USER` and the number of registered deposit tokens (attacker fills slots using listed collaterals obtained on-market with dust amounts). It cannot steal or permanently lock funds because removal paths (`withdraw` to zero, `repayAll`) still work, which caps severity at Medium.

### Recommendation
- Only mutate `depositTokensOfAccount`/`debtTokensOfAccount` for the *caller* (via `SynthContext._msgSender()`), not for transfer recipients; derive recipient state lazily or let recipients opt in.
- Alternatively, do not revert on the cap — wrap `debtPositionOf`/`depositOf` iteration behind a bounded helper and let the set grow, or add the token on `deposit`/`issue` only (pull-model) rather than on any token transfer.
- Or require a minimum balance threshold (e.g. > dust) before adding a token to an account's set.

### Proof of Concept
```solidity
// Foundry fork test against a deployed Pool
function test_dustGrief_tokenLimit() public {
    address victim = makeAddr("victim");
    address[] memory dtokens = pool.getDepositTokens(); // registered DepositTokens

    // 1. Attacker acquires dust of each deposit token's underlying and deposits
    //    (or buys the DepositToken on-market), then transfers 1 wei to victim.
    for (uint256 i; i < dtokens.length && i < MAX_PER_USER; ++i) {
        IERC20(dtokens[i]).transfer(victim, 1); // triggers set-add for victim
    }
    assertEq(pool.getDepositTokensOfAccount(victim).length, /* cap */);

    // 2. Victim tries to deposit a collateral type not yet in their set.
    vm.startPrank(victim);
    underlying.approve(address(depositToken), type(uint256).max);
    vm.expectRevert(); // TokensPerUserExceedLimit / cap revert in Pool
    depositToken.mint(1e18);
    // 3. Same for issuing a new synthetic debt token.
    vm.expectRevert();
    pool.issue(syntheticToken, 1e18);
    vm.stopPrank();
}
```
Reproduction note: verify on the deployed Pool whether the revert is enforced inside `_updateDepositTokensOfAccount`/`_updateDebtTokensOfAccount` (set-add exceeding `MAX_TOKENS_PER_USER`) and whether the number of registered deposit tokens plus attacker-controllable positions can reach the cap on the target chain's deployment.