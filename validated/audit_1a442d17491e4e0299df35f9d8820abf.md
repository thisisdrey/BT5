### Title
VesperGateway.deposit() lacks slippage protection on vToken minting, enabling sandwich/inflation attacks on the Vesper share rate - (File: contracts/VesperGateway.sol)

### Summary
`VesperGateway.deposit()` converts a user's `underlying` into Vesper `vToken` shares and then deposits those shares into the Metronome `DepositToken`, but it never enforces a minimum amount of vTokens (or msdTokens) the user must receive. Because the vToken share rate depends on external Vesper pool state that an unprivileged attacker can move (donation to inflate `pricePerShare`, or a same-block AMM/strategy interaction), a pending victim deposit can be executed at a worse exchange rate than the user expected, minting fewer `msdToken` collateral units for the same `amount_` of underlying.

### Finding Description
In `VesperGateway.deposit` (contracts/Vesper.sol, lines 44-65):

```solidity
uint256 _balanceBefore = vToken_.balanceOf(address(this));
vToken_.deposit(amount_);
uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;
...
_depositToken.deposit(_vTokenAmount, _msgSender);
```

- `vToken_.deposit(amount_)` mints Vesper shares at whatever rate prevails at execution time; there is no `minSharesOut`/`amountOutMin` parameter anywhere in the call path.
- The minted `_vTokenAmount` is passed directly into `IDepositToken.deposit`, which mints `msdToken` 1:1 net of fee (contracts/DepositToken.sol lines 211-237), so any loss in the vToken conversion is passed straight through to the user's collateral balance.
- `DepositToken.deposit` itself mints a deterministic `amount - fee`, so it is not independently manipulable — but `VesperGateway.deposit` is the user-facing entry point whose output depends on mutable external vault state.
- Analogous gap exists in `VesperGateway.withdraw` (lines 73-93): `vToken_.withdraw(_vTokenAmount)` is balance-delta measured and the resulting `_underlyingAmount` is forwarded with no minimum check.

Attack flow:
1. Victim submits `VesperGateway.deposit(pool_, vToken_, amount_)`.
2. Attacker front-runs by (a) depositing into the Vesper pool and (b) donating underlying to inflate the pool's `pricePerShare` / total assets (classic ERC-4626-style inflation; the report's bug class).
3. Victim's `vToken_.deposit(amount_)` mints fewer shares; `DepositToken.deposit` credits proportionally fewer `msdTokens`.
4. Attacker redeems their inflated vTokens, extracting a slice of the victim's deposit.

No modifier prevents this: only `isPoolRegistered` is enforced (line 45), `nonReentrant` is absent on `deposit` (and irrelevant anyway), and SynthContext/`_msgSender()` does not constrain ordering.

### Impact Explanation
Direct theft of user funds: the victim receives less collateral (`msdToken`) than the fair value of the underlying they supplied, and the difference accrues to the attacker via the inflated share redemption. The magnitude is bounded by the attacker's donation cost versus extractable value, typical of share-dilution sandwiches.

### Likelihood Explanation
Requires a victim to route deposits through `VesperGateway` and a Vesper pool whose share price is manipulable by donation or same-transaction liquidity moves. Vesper V2 pools compute share price from pooled value including strategies, so exploitability depends on whether donations actually raise `pricePerShare` for the specific deployed vToken; this must be verified on a fork. Where it holds, the attack needs only capital and mempool access — fully unprivileged.

### Recommendation
Add a `minSharesOut_` (or `minMsdOut_`) parameter to `VesperGateway.deposit` and revert if `_vTokenAmount` (or the returned `_deposited`) is below it; add a `minUnderlyingOut_` to `VesperGateway.withdraw` checked against `_underlyingAmount`.

### Proof of Concept
Fork test (Foundry, mainnet fork at a block with a registered pool + a whitelisted vToken collateral):

```solidity
// setup: pool, depositToken (vToken collateral), vesperGateway registered via PoolRegistry
IERC20 underlying = IERC20(vToken.token());

// attacker front-runs
underlying.approve(address(vToken), attackerSeed);
vToken.deposit(attackerSeed);              // attacker mints shares
deal(address(underlying), attacker, donation);
underlying.transfer(address(vToken), donation); // inflate pricePerShare

// victim deposits via gateway
vm.prank(victim);
underlying.approve(address(gateway), victimAmount);
vm.prank(victim);
gateway.deposit(pool, vToken, victimAmount);

// victim received fewer msdTokens than quoteDepositOut(victimAmount) implied
assertLt(depositToken.balanceOf(victim), expectedFairShares);

// attacker redeems inflated vTokens for a profit
vToken.withdraw(vToken.balanceOf(attacker));
```

Note: `Pool.swap`, `DebtToken.issue`/`repay`, and `DepositToken.deposit`/`withdraw` are all oracle- or fee-based 1:1 conversions with no exchange-rate input, so the analog does not apply to them directly; the reachable surface is the external-vault conversion inside `VesperGateway`. `SmartFarmingManager.leverage`/`flashRepay` already take `depositAmountMin_`/`swapAmountOutMin_` slippage parameters, confirming the codebase treats this protection as required — it is only missing on the Vesper gateway path.