### Title
A reverting price feed freezes liquidations, withdrawals and swaps for every position touching that asset — (`contracts/Pool.sol`)

### Summary
Metronome's `Pool` prices all collateral and debt through a single `masterOracle()` call chain (`quote` / `quoteTokenToUsd`) with no `try/catch` and no fallback oracle. If the oracle/provider registered for any one collateral or synthetic asset reverts (e.g., the upstream Chainlink feed is taken offline, or the pull provider's `price-too-behind`/`price-too-ahead` checks fire), every code path that values that asset — `debtPositionOf`, `liquidate`, `depositOf`, `withdraw`, `swap`, `issue` — reverts for all users holding it, until governance manually registers a new oracle.

### Finding Description
`Pool.liquidate` calls `debtPositionOf(account_)` to determine health, which iterates the account's deposit and debt token sets and calls `masterOracle().quoteTokenToUsd(...)` for each token. It then calls `quoteLiquidateOut`, which performs `masterOracle().quote(syntheticToken_, underlying_, amountToRepay_)` at `contracts/Pool.sol:456-460`. The debt-floor check at `contracts/Pool.sol:572-575` adds a third oracle dependency. None of these calls are wrapped — a single revert in the underlying price provider propagates and reverts the whole transaction.

The deployed `MasterOracle` (external to this repo, e.g. `0x80704Acdf97723963263c78F861F091ad04F46E2` on mainnet) dispatches per-token to registered providers; the repo's own integration tests confirm revert-driven DoS behavior: when the pull provider's price is outdated, `masterOracle.getPriceInUsd` reverts with `price-too-behind` (`test/E2E.mainnet.test.ts:1054`) and interaction is only possible after anyone pushes a price update (`test/E2E.mainnet.test.ts:1057-1076`). There is no fallback provider and no graceful degradation — the same applies if a push-based feed (Chainlink aggregator, `CHAINLINK_PRICE_PROVIDER` in `helpers/address.ts:98`) is disabled or starts reverting.

### Impact Explanation
While a token's oracle is unavailable:
- `Pool.liquidate` always reverts (both `debtPositionOf` and `quoteLiquidateOut` query the dead feed), so underwater positions involving that collateral/debt cannot be liquidated. If the asset's real price is simultaneously falling — the exact scenario in which Chainlink historically disables feeds — the protocol accumulates bad debt and can be pushed into insolvency.
- `depositOf`/`debtPositionOf`/`withdraw` revert for any account whose token set includes the affected asset, temporarily freezing user collateral.
- `swap` and `issue` revert when the affected token is involved.

Impact matches the accepted classes: temporary freezing of funds and protocol insolvency risk from frozen liquidations.

### Likelihood Explanation
The trigger is external (an upstream feed reverting or going stale), not attacker-initiated — which caps severity at Medium. It requires no malicious privileged role; Chainlink has disabled feeds in extreme market conditions before, and the pull-oracle providers the protocol integrates revert autonomously on stale prices. Recovery requires the MasterOracle governor to register a replacement oracle, extending the freeze window.

### Recommendation
- Wrap oracle reads in `try/catch` inside `MasterOracle` (or a per-asset fallback provider) so that a primary feed failure falls back to a secondary source instead of reverting.
- Alternatively, allow `debtPositionOf`/`liquidate` to skip or conservatively price collateral whose feed is unavailable (e.g., treat collateral as 0 value so liquidation of the debt side still proceeds, while blocking new borrows).
- Minimize the number of oracle calls per transaction so a single dead feed doesn't block operations on unrelated assets.

### Proof of Concept
Fork test (Foundry): on a mainnet fork, create an unhealthy position, then `vm.mockCallRevert` or point the token's oracle at a reverting provider, and observe `Pool.liquidate` revert:

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IMasterOracle {
    function getPriceInUsd(address) external view returns (uint256);
    function quote(address, address, uint256) external view returns (uint256);
    function updateTokenOracle(address token_, address oracle_) external;
}

interface IPool {
    function liquidate(address synth, address account, uint256 amountToRepay, address depositToken) external;
    function debtPositionOf(address account) external view returns (bool _isHealthy, uint256, uint256, uint256, uint256);
}

contract RevertingOracle {
    function getPriceInUsd(address) external pure returns (uint256) { revert("oracle-down"); }
}

contract OracleDownDoS_Test is Test {
    IMasterOracle masterOracle = IMasterOracle(0x80704Acdf97723963263c78F861F091ad04F46E2); // mainnet MasterOracle
    IPool pool = IPool(0x574a32f1047C631653D9283d36e73cF9BA67B940); // Pool_2
    address constant MASTER_ORACLE_GOVERNOR = 0x9520b477Aa81180E6DdC006Fc09Fb6d3eb4e807A;

    function test_liquidationDoS_whenOracleReverts() public {
        // 1. Pick an unhealthy position <account, synth, depositToken> (or create one via price move)

        // 2. Simulate the collateral's price feed going down:
        //    MasterOracle governor re-registers the token to a reverting provider,
        //    equivalent to the upstream Chainlink feed starting to revert.
        vm.prank(MASTER_ORACLE_GOVERNOR);
        masterOracle.updateTokenOracle(COLLATERAL_UNDERLYING, address(new RevertingOracle()));

        // 3. Even a simple health check reverts:
        vm.expectRevert("oracle-down");
        pool.debtPositionOf(UNDERWATER_ACCOUNT);

        // 4. Any liquidate() call reverts — bad debt cannot be cleared:
        vm.expectRevert("oracle-down");
        pool.liquidate(MS_SYNTH, UNDERWATER_ACCOUNT, amountToRepay, DEPOSIT_TOKEN);
    }
}
```

Equivalent reproduction exists in-repo for pull oracles without any mocking: `test/E2E.mainnet.test.ts:1054` shows `getPriceInUsd` reverting with `price-too-behind` when prices are stale, and every oracle-dependent `Pool` function reverts identically until a price update is pushed.