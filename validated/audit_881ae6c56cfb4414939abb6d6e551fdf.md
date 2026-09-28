### Title
`VesperGateway.withdraw` permanently strands unburned vToken shares on the gateway when the Vesper pool under-fulfills a withdrawal — (File: contracts/VesperGateway.sol)

### Summary
`VesperGateway.withdraw` pulls `amount_` msdTokens from the caller, burns all of them via `IDepositToken.withdraw`, receives `_vTokenAmount` vTokens, and calls `vToken_.withdraw(_vTokenAmount)`. It measures only the *underlying* delta and forwards that to the user (`contracts/VesperGateway.sol:73-93`). `IVPool.withdraw` returns `void` (`contracts/interfaces/external/IVPool.sol:12`), and Vesper `VPool` withdrawals can under-fulfill — burning fewer shares than requested and returning proportionally less underlying — when pool liquidity is squeezed (high utilization in the underlying lending market), which is exactly the Yearn partial-withdrawal scenario from the reference report. Any vToken shares left unburned stay on `VesperGateway`, whose only recovery path is `TokenHolder.sweep` gated by `onlyGovernor` (`contracts/VesperGateway.sol:100-102`) — there is no mechanism to return them to the user who paid for them.

### Finding Description
In `withdraw` (`contracts/VesperGateway.sol:73-93`):

1. The full `amount_` of msdTokens is taken from the caller and burned inside `_depositToken.withdraw(amount_, address(this))`; the gateway receives `_vTokenAmount` vTokens.
2. `vToken_.withdraw(_vTokenAmount)` is fire-and-forget — the return value/burned-share count is never read (the interface doesn't even expose it).
3. Only the underlying delta is forwarded: `_underlying.safeTransfer(_msgSender, _underlyingAmount)`.

If `vToken_.withdraw` burns `sharesBurned < _vTokenAmount` (Vesper pools free what they can from strategies and leave the rest of the shares), the residual `_vTokenAmount - sharesBurned` vTokens remain on the gateway forever — the user's msdTokens were burned in full, but part of the corresponding collateral is stranded in a contract that has no user-facing refund path (`_requireCanSweep` is `onlyGovernor`).

### Impact Explanation
Users' collateral (vTokens representing claim on underlying) becomes permanently frozen on `VesperGateway` for any unprivileged user; no public function lets them reclaim the residual shares. The funds are only recoverable via a privileged governor sweep — an administrative rescue, not protocol logic — so for users this is a permanent loss of the unburned share portion of their deposit.

### Likelihood Explanation
The trigger is a liquidity shortage in the Vesper pool at withdrawal time — a routine market condition (high utilization of the underlying lending market), not an attacker prerequisite. Additionally, an attacker can worsen the odds by maximizing utilization of the Vesper pool's underlying (borrowing available liquidity via flash loan / leveraged borrow) before victims' withdrawals land, though victim behavior is required for loss. Modifiers don't prevent it: `nonReentrant` doesn't help, `onlyGovernor` only restricts `sweep`, and nothing checks the gateway's post-withdraw vToken balance.

### Recommendation
Mirror the YearnProvider fix: snapshot the gateway's vToken balance before `vToken_.withdraw` and refund the excess to `_msgSender` afterward, e.g.

```solidity
uint256 _vTokenBefore = vToken_.balanceOf(address(this));
vToken_.withdraw(_vTokenAmount);
uint256 _vTokenLeft = vToken_.balanceOf(address(this)) - (_vTokenBefore - _vTokenAmount);
if (_vTokenLeft > 0) vToken_.safeTransfer(_msgSender, _vTokenLeft);
```

Equivalently, wrap vToken withdrawal to only burn the shares actually consumed.

### Proof of Concept
A Hardhat fork test (mainnet fork with a real vaUSDC pool, or a `VPoolMock` variant whose `withdraw` burns fewer shares than requested to emulate partial withdrawal):

1. Fork mainnet; deploy/use `VesperGateway` with a registered `Pool` and `msdVaUSDC` DepositToken whose underlying is vaUSDC.
2. User calls `vesperGateway.deposit(pool, vaUSDC, amount)` to obtain msdVaUSDC.
3. Deplete vaUSDC pool liquidity (borrow max available underlying from the strategies' lending market, or in the mock, make `withdraw` burn only 60% of shares and emit the corresponding underlying).
4. User calls `vesperGateway.withdraw(pool, vaUSDC, userMsdBalance)` and approves msdVaUSDC to the gateway.
5. Assert: user's entire msdVaUSDC balance is burned; user receives only the partially-available underlying; `vaUSDC.balanceOf(vesperGateway) > 0` equals the unburned shares; no user-callable function retrieves them (only `sweep` via `governor`).

Uncertainty note: whether a given deployed Vesper `VPool` under-burns shares versus reverting on liquidity shortage is pool-version dependent and could not be fully verified from this repo alone (the bundled `IVPool` only declares `withdraw(uint256)` returning void); the PoC should confirm behavior against the specific deployed vToken used as collateral. If the deployed pool reverts instead of under-fulfilling, the impact degrades to a temporary withdrawal DoS rather than frozen shares.