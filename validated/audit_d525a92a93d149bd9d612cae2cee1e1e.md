### Title
Permissionless `initialize()` lets anyone front-run deployment and seize control of `PoolRegistry` (oracle + fee collector) - (File: contracts/PoolRegistry.sol)

### Summary
Metronome's upgradeable contracts expose public `initialize()` functions protected only by OpenZeppelin's `initializer` modifier — the same bug class as the Allo `initialize()` report. The strongest surface is `PoolRegistry.initialize(IMasterOracle masterOracle_, address feeCollector_)`, which is callable by any EOA the moment the proxy is deployed and before the legitimate initialization transaction lands. It atomically sets `masterOracle` and `feeCollector` with no owner/governor check.

### Finding Description
`PoolRegistry` is deployed behind a proxy (implementation constructor calls `_disableInitializers()` at contracts/PoolRegistry.sol:79-81, so the implementation itself is safe). The proxy, however, relies on `initialize()` being invoked in the same transaction as deployment. The function at contracts/PoolRegistry.sol:83-90:

```solidity
function initialize(IMasterOracle masterOracle_, address feeCollector_) external initializer {
    if (address(masterOracle_) == address(0)) revert OracleIsNull();
    if (feeCollector_ == address(0)) revert FeeCollectorIsNull();
    __Pauseable_init();
    masterOracle = masterOracle_;
    feeCollector = feeCollector_;
```

contains no `onlyGovernor`/owner check — whoever calls it first supplies `masterOracle_` and `feeCollector_`. `masterOracle` is the single price source used by `Pool` for collateral/debt valuation, health-factor checks, swap quotes, and liquidation bounds. The identical pattern exists in `FeeProvider.initialize(IPoolRegistry, IESMET)` (contracts/FeeProvider.sol:60-70), which lets an attacker point the fee module at a fake `PoolRegistry` whose `governor()` returns the attacker, granting control over all fee parameters and esMET-gated discounts, and in `initialize()` functions on `Pool`, `DebtToken`, `DepositToken`, `SyntheticToken`, `RewardsDistributor`, `SmartFarmingManager`, `Treasury`, and `AMO`, all guarded only by `initializer`.

### Impact Explanation
If an attacker front-runs `PoolRegistry.initialize`, they install a malicious `IMasterOracle`. With full control of reported prices the attacker can deposit a worthless collateral token, have the oracle report an inflated price, and borrow real synthetic assets against it — draining the pool and causing protocol insolvency (direct theft of user funds). Alternatively the attacker sets `feeCollector` to themselves to skim protocol fees, or simply griefs deployment. Because `initializer` permanently locks the contract after the first call, the legitimate team cannot re-initialize; the only recourse is redeploying the proxy, and if funds were already deposited the theft is unrecoverable.

### Likelihood Explanation
Exploitation requires the proxy to be observable on-chain in an uninitialized state, i.e., deployment and `initialize` are not atomic in one transaction. This is a well-known real-world failure mode (the exact Allo finding), and Metronome's deploy setup grants no protection beyond deployment-script atomicity — there is no on-chain access control as a second line of defense. The constructor's `_disableInitializers()` protects only the logic contract, not the proxy storage where `initialize` writes. Likelihood is therefore tied to a deployment-time race window rather than an always-open path, which is why the impact is high but the likelihood conditional — consistent with a Medium.

### Recommendation
Gate the initializer behind a privileged caller. Options: pass a `governor_`/`owner_` argument into `initialize` combined with an immutable deployer check, or more robustly set a deployer/owner in the proxy constructor (or via `TransparentUpgradeableProxy` admin) and require `msg.sender == owner` inside `initialize`. Alternatively, hard-code atomicity by performing `initialize` inside the deployment transaction in the deploy scripts and verifying on-chain that `masterOracle`/`feeCollector`/`_poolRegistry` are the intended values before wiring any dependent contracts.

### Proof of Concept
Foundry fork test sketch (any chain where a fresh `PoolRegistry` proxy is deployed, or a local reproduction):

```solidity
function test_PoolRegistryInitializeFrontRun() public {
    // 1. Team deploys implementation + proxy, initialize tx is pending
    PoolRegistry impl = new PoolRegistry();
    ERC1967Proxy proxy = new ERC1967Proxy(address(impl), "");
    PoolRegistry registry = PoolRegistry(address(proxy));

    // 2. Attacker sees pending initialize() in mempool and front-runs it
    address attacker = address(0xbad);
    MaliciousOracle evilOracle = new MaliciousOracle(); // implements IMasterOracle
    vm.prank(attacker);
    registry.initialize(IMasterOracle(address(evilOracle)), attacker);

    // Attacker's oracle now prices all collateral/debt
    assertEq(address(registry.masterOracle()), address(evilOracle));
    assertEq(registry.feeCollector(), attacker);

    // 3. Legitimate initialize reverts — DoS on correct config
    vm.expectRevert(); // Initializable: contract is already initialized
    registry.initialize(realOracle, realFeeCollector);

    // 4. Attacker prices a dust token at $1M, deposits it into a Pool,
    //    and borrows the pool's real synthetics -> insolvency.
}
```

The same pattern applies to `FeeProvider.initialize` — a fork PoC deploys the `FeeProvider` proxy, front-runs `initialize` with a fake `IPoolRegistry` returning `governor() == attacker`, then calls `updateDepositFee`/`updateSwapFee` successfully, demonstrating takeover of all fee governance.

One caveat: I confirmed the permissionless-initializer pattern in `PoolRegistry` and `FeeProvider` directly, and the `initializer`-only pattern across the other upgradeable contracts by search; I did not read every one of their `initialize` bodies line-by-line, so the exact parameters settable by the attacker in `Pool`, `Treasury`, `SmartFarmingManager`, etc., should be confirmed — but `PoolRegistry` alone is sufficient for a high-impact theft path.