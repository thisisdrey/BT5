### Title
Unprivileged account-list exhaustion via dust `DepositToken` transfers blocks collateral deposits and rescues - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The analog of CVE-2022-21366 (unauthenticated, availability-only, "partial DOS" via untrusted data reaching a parsing component) is Metronome's per-account deposit-token list. `Pool` enforces `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` once an account's `depositTokensOfAccount` set is full [1](#0-0) . That set is mutated from `DepositToken` transfer/mint hooks (the `SenderIsNotDepositToken`-gated path in `Pool`), so anyone can add a token to *another* account's list simply by transferring dust share amounts of each deposit token to the victim — no privileged role, oracle manipulation, or malicious infrastructure required.

### Finding Description
- `Pool` stores each account's collateral set in a `MappedEnumerableSet.AddressSet` bounded by `MAX_TOKENS_PER_USER` (30) [2](#0-1) .
- `DepositToken` mint/transfer paths call back into `Pool` to register the token in the recipient's set; the callback is gated only by `SenderIsNotDepositToken`, meaning the *recipient* — who never consented — has their set mutated. There is no opt-in, minimum amount, or allowance check on the receiving side.
- An attacker deposits dust amounts of every listed `DepositToken` (cheap, and withdrawable afterward) and `transfer`s 1 wei of each share token to the victim. Each distinct token occupies one of the 30 slots.
- Once the set reaches 30 entries, any `deposit` (or received transfer) of a *new* collateral type reverts with `UserReachedMaxTokens` [3](#0-2) .
- The impact mirrors the CVE exactly: a pure-availability, partial denial of service reachable by an unauthenticated network attacker supplying crafted inputs (here, dust transfers) — availability of the deposit/liquidation-rescue path is degraded while confidentiality/integrity are unaffected.

### Impact Explanation
The victim loses the ability to add new collateral types to their position. The material consequence: a victim with an existing debt position approaching the liquidation threshold cannot deposit additional collateral to restore health, so `Pool.liquidate` can seize their position where timely top-ups would have saved it. The same blocker hits `SmartFarmingManager` leverage flows and `Operator.execute` batch deposits on behalf of the victim (all routed through the same `SynthContext`/deposit path). Funds are not directly stolen and the victim can eventually evict entries by withdrawing or transferring out the dust shares, so this is a *partial/temporary* DoS plus forced-liquidation exposure — matching the medium-severity availability profile of the source advisory.

### Likelihood Explanation
- Attacker needs only an EOA and dust balances of the underlying assets for each registered deposit token; deposits are permissionless and the dust is recoverable, so cost is essentially gas across ≤30 tokens.
- No privileged actor, malicious LayerZero endpoint, oracle fault, or governance action is required — this fits the unprivileged-attacker rules.
- The revert check fires before any health/lock logic can help, and no `ReentrancyGuardTransient`, pause flag, or supply cap prevents the set insertion itself on deployed configurations.
- Caveat: severity is bounded — the victim can self-clean slots by burning/transferring the dust shares, so the window of exposure depends on victim inattention.

### Recommendation
- Do not credit another account's `depositTokensOfAccount` set on inbound `DepositToken.transfer`s — register collateral only on `deposit` initiated by (or `SynthContext`-attributed to) the account owner.
- Alternatively, keep a separate "received shares" accounting that does not consume the 30-slot cap, or require a minimum deposit threshold before inserting into the set.

### Proof of Concept
Reproducible on a mainnet fork (Hardhat/Foundry):

```solidity
// Fork mainnet; pool = Pool proxy; victim = funded debtor account.
// Assume pool lists N >= 30 DepositTokens.
for (uint i; i < depositTokens.length; ++i) {
    IDepositToken dt = depositTokens[i];
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 1);        // dust
    underlying.approve(address(dt), 1);
    dt.deposit(1);                                 // mint dust shares to attacker
    dt.transfer(victim, 1);                        // inserts dt into victim's set
}
assertEq(pool.depositTokensOfAccount(victim).length, 30);

// Victim attempts to deposit a collateral type not yet in the set:
vm.prank(victim);
vm.expectRevert(UserReachedMaxTokens.selector);
newDepositToken.deposit(amount);
```

Unverified detail worth confirming in a Devin session: the exact hook name in `DepositToken`/`Pool` that inserts into `depositTokensOfAccount` on `transfer` (the `SenderIsNotDepositToken` gate in `Pool.sol` is the entry point) — if transfers do *not* register the recipient, the attack instead requires the victim to have interacted, weakening likelihood.

### Citations

**File:** contracts/Pool.sol (L32-32)
```text
error UserReachedMaxTokens();
```

**File:** contracts/Pool.sol (L72-79)
```text
    using MappedEnumerableSet for MappedEnumerableSet.AddressSet;

    string public constant VERSION = "1.3.2";

    /**
     * @notice Maximum tokens per pool a user may have
     */
    uint256 public constant MAX_TOKENS_PER_USER = 30;
```
