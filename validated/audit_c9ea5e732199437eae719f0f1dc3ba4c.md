### Title
`VesperGateway.deposit()` lacks slippage protection on the underlying→vToken→msdToken conversion, letting an attacker sandwich users via VPool share-price manipulation - ([File: contracts/VesperGateway.sol#L44-L65](contracts/VesperGateway.sol))

### Summary
`VesperGateway.deposit()` converts user-supplied `underlying` into `vToken` shares via an external `VPool`, then deposits those shares into `DepositToken.deposit()`, which mints `msdTOKEN` 1:1 (minus a fixed fee). The function accepts no `minSharesOut`/`minDepositOut` parameter and performs no slippage check on the amount of `vToken` received, so the number of `msdTokens` the user ultimately receives is entirely determined by the VPool's share price at execution time.

### Finding Description
The flow in `VesperGateway.deposit()` is:

```solidity
// contracts/VesperGateway.sol
_underlying.safeTransferFrom(_msgSender, address(this), amount_);
_underlying.safeApprove(address(vToken_), amount_);
uint256 _balanceBefore = vToken_.balanceOf(address(this));
vToken_.deposit(amount_);                              // no minOut
uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;
IDepositToken _depositToken = pool_.depositTokenOf(vToken_);
vToken_.safeApprove(address(_depositToken), _vTokenAmount);
_depositToken.deposit(_vTokenAmount, _msgSender);      // mints msdTOKEN 1:1
```

`DepositToken.deposit()` (`contracts/DepositToken.sol#L211-L237`) mints `_deposited` purely from the token amount it receives (`quoteDepositOut` applies only the flat `depositFee` — there is no exchange-rate normalization), so any loss in `underlying → vToken` conversion is passed straight through to the user as fewer `msdTokens`.

`vToken_` and `pool_` are caller-supplied arguments; the only check is `_poolRegistry.isPoolRegistered(address(pool_))`. The number of shares minted by `vToken_.deposit(amount_)` depends on the Vesper pool's `pricePerShare`, which is `poolValue / totalSupply`. An unprivileged attacker can move `poolValue` within the same block by donating `underlying` directly to the VPool/strategies (Vesper pools treat idle + strategy-held assets as pool value), inflating `pricePerShare` right before the victim's transaction executes.

Attack sequence:
1. Victim submits `vesperGateway.deposit(pool, vToken, X)` expecting ~`Y` vTokens / `Y` msdTokens.
2. Attacker front-runs with a donation of `D` underlying into the VPool, raising `pricePerShare`.
3. Victim's `vToken_.deposit(X)` mints proportionally fewer vTokens; `deposit()` mints proportionally fewer `msdTokens` to the victim.
4. Attacker's own vToken position appreciates by roughly the donated amount minus the victim's lost share-value; the victim's loss (the gap between expected and actual shares) accrues to all existing vToken holders, including the attacker.

Unlike `SmartFarmingManager.leverage()` and `flashRepay()` — which both expose `depositAmountMin_`/`swapAmountOutMin_` slippage parameters (`contracts/SmartFarmingManager.sol#L98-L144`, `#L155-L211`) — `VesperGateway.deposit()` has no equivalent. Notably `deposit()` also lacks the `nonReentrant` guard present on `withdraw()`.

### Impact Explanation
Direct loss of user funds: a user depositing through the gateway mints `msdToken` collateral worth less than the `underlying` they supplied, with the difference captured by vToken holders (including the attacker). The `msdToken` is collateral recognized by the Pool's oracle, so the user also permanently locks a smaller collateral position than intended. The SafEth analog holds: a state variable (here external `pricePerShare`, there `preDepositPrice`) that an unprivileged actor can move between tx submission and execution determines how many shares the staker/depositor receives, and no `minOut` lets the user bound that.

### Likelihood Explanation
- `deposit()` is a public, permissionless entry point; the attacker needs no roles.
- Only requires mempool visibility (mainnet) and capital equal to a donation meaningful relative to VPool TVL; Vesper pools historically accept direct `underlying` transfers that count toward pool value.
- On deployed chains (mainnet, optimism, base) where Vesper `vaUSDC`/`vaETH` are real collateral, share-price movement of a few percent is achievable for large deposits.
- Profitability is bounded by the victim's loss accruing pro-rata to the attacker's shareholding, so it is strongest when the attacker holds or flash-acquires a meaningful share position; even when unprofitable it is an unmitigated griefing/sandwich vector that the codebase's own conventions (leverage/flashRepay slippage params) treat as required.

### Recommendation
Add a user-supplied minimum-output parameter to `VesperGateway.deposit()` (and the `IVesperGateway` interface), enforcing it on the vToken amount received and/or the final `msdToken` minted:

```diff
- function deposit(IPool pool_, IVPool vToken_, uint256 amount_) external override {
+ function deposit(IPool pool_, IVPool vToken_, uint256 amount_, uint256 minVTokenOut_) external override {
      ...
      uint256 _vTokenAmount = vToken_.balanceOf(address(this)) - _balanceBefore;
+     if (_vTokenAmount < minVTokenOut_) revert SlippageTooHigh();
      ...
      (uint256 _deposited,) = _depositToken.deposit(_vTokenAmount, _msgSender);
+     // optionally: if (_deposited < minDepositOut_) revert ...
}
```

Add `nonReentrant` to `deposit()` for consistency with `withdraw()`, and surface the slippage parameter in the UI using `vToken` `pricePerShare` quotes.

### Proof of Concept
Hardhat mainnet-fork outline (mirroring `test/E2E.mainnet.test.ts` `should deposit vaUSDC using USDC`):

```ts
// fork mainnet; use deployed poolRegistry, pool_1, msdVaUSDC, vaUSDC, usdc
const vesperGateway = await (await ethers.getContractFactory('VesperGateway', alice))
  .deploy(poolRegistry.address)

const amount6 = parseUnits('10000', 6) // 10k USDC victim deposit

// baseline quote
const ppsBefore = await vaUSDC.pricePerShare() // or balanceDelta quote
await usdc.connect(alice).approve(vesperGateway.address, amount6)

// ATTACKER front-run: donate underlying to raise vaUSDC pricePerShare
await usdc.connect(attacker).transfer(vaUSDC.address, parseUnits('5000', 6))
// (or deposit then donate; donation counts toward poolValue)

const ppsAfter = await vaUSDC.pricePerShare()
expect(ppsAfter).gt(ppsBefore)

await vesperGateway.connect(alice).deposit(pool_1.address, vaUSDC.address, amount6)
const received = await msdVaUSDC.balanceOf(alice.address)

// compare vs. deposit without donation on a snapshot
const receivedBaseline = /* same call on clean snapshot */
expect(received).lt(receivedBaseline) // victim minted fewer msdTokens, no revert possible
```

The delta between `received` and `receivedBaseline` is the uncompensated user loss; `DepositToken.deposit` mints strictly 1:1 minus the fixed `depositFee`, so the entire share-price haircut is borne by the depositor.