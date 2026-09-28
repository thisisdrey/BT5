### Title
Unprivileged attacker can dust-fill a victim's `depositTokensOfAccount` set to `MAX_TOKENS_PER_USER`, DoS-ing the victim's deposits and transfers-in - (File: contracts/DepositToken.sol)

### Summary
`DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever a recipient's balance moves from zero to non-zero. `Pool.addToDepositTokensOfAccount` enforces a per-account cap (`MAX_TOKENS_PER_USER` in `contracts/Pool.sol`) on the `depositTokensOfAccount` enumerable set (`contracts/storage/PoolStorage.sol:78`). Because deposit tokens are freely transferable ERC20s, an unprivileged attacker can deposit dust of every listed collateral and `transfer()` 1 wei of each deposit token to a victim, filling their account set to the cap. Every subsequent `deposit(..., onBehalfOf_=victim)` or `transfer`/`seize` into the victim that would introduce a *new* deposit token reverts on the cap check, denying service — the direct analog of a crafted ioctl crashing the kernel: a crafted call sequence that bricks a subsystem for its target.

### Finding Description
- `_mint` (called by `deposit`, `Pool.liquidate` fee path, `SmartFarmingManager`) and `_transfer` (called by `transfer`, `transferFrom`, `seize`) register the token in the victim's per-account set on first receipt (`contracts/DepositToken.sol:486-488`, `contracts/DepositToken.sol:517-520`).
- Removal only happens when a balance returns to zero (`_burn` / `_transfer`, lines 460-462 and 523-525).
- The attacker path: `deposit(dust, attacker)` per collateral → `transfer(victim, 1)` for each of the pool's deposit tokens → victim's `depositTokensOfAccount` hits `MAX_TOKENS_PER_USER` → `victim`'s next deposit of a *new* collateral type (or any inbound transfer/seizure of a token they don't hold) reverts.
- No modifier stops this: `transfer` only checks the *sender's* unlocked balance via `_revertIfLocked(_msgSender, amount_)` (line 350); there is no opt-in or recipient check, no minimum-amount floor, and `SynthContext`/pause flags do not apply to plain ERC20 transfers.

### Impact Explanation
Temporary freezing of funds/liveness: the victim cannot deposit new collateral types, cannot receive deposit tokens of types they don't already hold, and — critically — `Pool.liquidate`/`seize` payouts or `SmartFarmingManager` unwind flows that would mint them a new token type revert, which can also block liquidations where the liquidator is a fresh account. The DoS persists until the victim manually sweeps each dust token to zero-balance, paying gas per entry. Attack cost is bounded by the dust deposit across the pool's registered collaterals; no privileged role, oracle manipulation, or malicious endpoint is required.

### Likelihood Explanation
Likelihood is moderate: it requires the pool to list enough collaterals to reach `MAX_TOKENS_PER_USER` and the attacker must supply (dust amounts of) each underlying. Impact is capped at temporary denial — the victim can self-remediate by transferring each dust balance fully out (returning the balance to zero removes the set entry) — which is why this maps to a Medium availability bug rather than a fund-theft issue, mirroring the CVE's Medium CVSS crash-only impact.

### Recommendation
- Do not add deposit tokens to `depositTokensOfAccount` on plain `transfer`/`transferFrom`; only register on `deposit`/`_mint` (position-establishing flows), or make registration opt-in.
- Alternatively, ignore transfers below a dust threshold, or allow removal/registration independent of the transferable ERC20 path so third parties cannot mutate a user's account set.

### Proof of Concept
```solidity
// Foundry fork test sketch
// assume pool has >= MAX_TOKENS_PER_USER registered DepositTokens
address victim = makeAddr("victim");
for (uint i; i < depositTokens.length; ++i) {
    DepositToken dt = DepositToken(depositTokens[i]);
    IERC20 underlying = dt.underlying();
    deal(address(underlying), attacker, 10);           // dust underlying
    vm.startPrank(attacker);
    underlying.approve(address(dt), 10);
    dt.deposit(10, attacker);                          // mint dust msdTOKEN
    dt.transfer(victim, 1);                            // adds dt to victim's set
    vm.stopPrank();
}
// victim's depositTokensOfAccount is now at MAX_TOKENS_PER_USER

// Victim tries to deposit a collateral type they don't hold -> reverts
DepositToken newDt = DepositToken(newlyListedOrUnusedToken);
deal(address(newDt.underlying()), victim, 1 ether);
vm.startPrank(victim);
newDt.underlying().approve(address(newDt), 1 ether);
vm.expectRevert(); // cap exceeded in addToDepositTokensOfAccount
newDt.deposit(1 ether, victim);

// Same for anyone transferring a fresh deposit token to victim, and for
// seize() targeting a token victim doesn't hold during liquidation.
```