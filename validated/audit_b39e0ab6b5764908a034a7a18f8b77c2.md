### Title
Dust deposits/transfers fill a victim's `MAX_TOKENS_PER_USER` slot list and DoS their deposits and issuance - (File: contracts/Pool.sol)

### Summary
The Strata bug is a per-user capped request queue that an attacker can fill with cheap third-party-initiated operations, blocking the victim's own finalizations. The Metronome analog is the per-account deposit-token list in `Pool` (`MappedEnumerableSet.AddressSet`, capped by `MAX_TOKENS_PER_USER` in `PoolStorage.sol`). Any new deposit token credited to an account pushes an entry into that account's set, and the pool reverts once the cap is reached. Because `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone deposit collateral for an arbitrary receiver, and `DepositToken.transfer` credits the recipient, an unprivileged attacker can spam dust amounts of every registered deposit token to a victim and permanently occupy their slot list.

### Finding Description
`Pool` tracks, per account, the set of deposit tokens it holds via `MappedEnumerableSet` in `contracts/lib/MappedEnumerableSet.sol:22-32`. The cap `MAX_TOKENS_PER_USER` is enforced in `Pool` when a new token is added to an account's set: adding beyond the cap reverts (`Pool.sol` / `PoolStorage.sol`). Two permissionless paths credit a third party:

- `DepositToken.deposit(uint256 amount_, address onBehalfOf_)` mints deposit tokens to `onBehalfOf_`, which registers the token in the receiver's set (`contracts/DepositToken.sol`).
- `DepositToken._transfer`/transfers to a fresh holder also add the token to the recipient's set (`contracts/DepositToken.sol`).

An attacker can therefore, for each of the N registered deposit tokens in a pool, deposit 1 wei of underlying on behalf of the victim (or transfer dust deposit tokens), filling all `MAX_TOKENS_PER_USER` slots. Afterwards:

- The victim cannot deposit into any *new* deposit token (revert on set add).
- `DebtToken.issue`/leverage flows that would register additional collateral are blocked.
- Entries only leave the set when the victim's balance of that token goes to zero; the victim must withdraw each dust position, and the attacker can refill slots faster than the victim clears them (each refill costs the attacker only dust + gas, matching the Strata asymmetry where 40 cheap requests DoS the victim's queue).

This mirrors the Strata report exactly: an external, unprivileged party consumes a scarce per-account slot on behalf of a victim without the victim's consent.

### Impact Explanation
Temporary-to-indefinite freezing of the victim's ability to add collateral and mint debt in any token they do not already hold. Existing positions remain withdrawable (withdrawal only removes set entries), so this is not theft, but it is a liveness violation of `deposit`, `issue`, `leverage`, and `swap`-into-new-collateral paths for as long as the attacker maintains the fill. Against a victim attempting to top up collateral to avoid liquidation, this DoS can force their position under-water, converting the freeze into indirect loss.

### Likelihood Explanation
The attack requires only an EOA, dust amounts of each listed collateral (e.g., wei of MET/WETH/wstETH variants), and gas for `MAX_TOKENS_PER_USER` transactions. No privileged role, oracle manipulation, or timing dependency is needed: `deposit` is permissionless while the pool is not paused, and `onBehalfOf_` is arbitrary. The constraint that only *registered* deposit tokens count limits the attack to the number of listed collaterals, but the cap is sized for that same list, so filling it is feasible whenever the list size approaches the cap.

### Recommendation
- Do not add a token to an account's set on behalf of another account, or make the cap apply only to entries the account itself opened; alternatively allow permissionless removal/ignore of dust entries below a threshold.
- Require `onBehalfOf_` deposits to pull the deposit-token receipt to `msg.sender` first (then explicit transfer), so slot consumption is always a deliberate act of the holder.
- If keeping the current design, exempt the set-add revert for tokens the account already holds and let victims evict zero/dust-balance entries via a `removeToken` function.

### Proof of Concept
A Foundry/Hardhat fork test sketch (to be run against deployed pool on Base/mainnet):

```solidity
// Fork: base mainnet
function test_FillVictimTokenList() public {
    IPool pool = IPool(DEPLOYED_POOL);
    address victim = makeAddr("victim");
    address attacker = makeAddr("attacker");

    // Enumerate registered deposit tokens
    address[] memory tokens = pool.getDepositTokens(); // or DepositToken[] via registry

    // Attacker deposits 1 wei of each underlying on behalf of victim
    for (uint256 i; i < tokens.length; ++i) {
        DepositToken dt = DepositToken(tokens[i]);
        IERC20 underlying = IERC20(dt.underlying());
        deal(address(underlying), attacker, 1);
        vm.startPrank(attacker);
        underlying.approve(address(dt), 1);
        dt.deposit(1, victim); // credits victim, registers token in victim's set
        vm.stopPrank();
    }

    assertEq(pool.depositTokensOf(victim).length, pool.MAX_TOKENS_PER_USER());

    // Victim tries to deposit into a new collateral type -> reverts
    DepositToken newDt = DepositToken(ANOTHER_TOKEN);
    IERC20 newUnderlying = IERC20(newDt.underlying());
    deal(address(newUnderlying), victim, 1 ether);
    vm.startPrank(victim);
    newUnderlying.approve(address(newDt), 1 ether);
    vm.expectRevert(); // TooManyDepositTokens / equivalent
    newDt.deposit(1 ether, victim);
    vm.stopPrank();
}
```

Caveat: I was unable to fully trace the exact revert error name and whether `DepositToken.transfer` (vs. only `deposit`) also triggers the set-add in this index; the deposit path via `onBehalfOf_` alone is sufficient for the attack since the attacker bears the dust cost either way. The set-add and cap logic lives in `contracts/Pool.sol` and `contracts/storage/PoolStorage.sol`; reviewers should confirm the cap value and that no sweep/removal helper exists on the deployed configuration.