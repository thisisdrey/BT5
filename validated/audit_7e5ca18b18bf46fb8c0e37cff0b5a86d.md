### Title
Permissionless `initialize` lets anyone frontrun deployment and become `governor` - ([File: contracts/PoolRegistry.sol](contracts/PoolRegistry.sol))

### Summary
The reported bug class is an unguarded post-deployment setup function that grants a privileged role to whoever calls it first. In Metronome, every upgradeable core contract (`PoolRegistry`, `Pool`, `DepositToken`, `DebtToken`, `RewardsDistributor`, `FeeProvider`, `SyntheticToken`, `SmartFarmingManager`, `AMO`) exposes a `public`/`external` `initialize()` with no caller restriction, and through `__Pauseable_init` → `__Governable_init` it assigns `governor = msg.sender` — the protocol's most powerful role. An attacker monitoring the mempool can frontrun the legitimate `initialize` call on a freshly deployed proxy and seize `governor` before the deployer.

### Finding Description
`PoolRegistry.initialize` is callable by anyone on an uninitialized proxy. It invokes `__Pauseable_init()`, which calls `__Governable_init()`, setting `governor` to the caller. The same pattern holds for `Pool.initialize` (calls `__Pauseable_init`), and `AMO.initialize` / `SmartFarmingManager.initialize` which set privileged state. `Governable.transferGovernorship` is `onlyGovernor`, so once an attacker is governor there is no recovery path short of redeploying.

Relevant code:

```solidity
// contracts/PoolRegistry.sol:83-93
function initialize(IMasterOracle masterOracle_, address feeCollector_) external initializer {
    ...
    __Pauseable_init();
    masterOracle = masterOracle_;
    feeCollector = feeCollector_;
    nextPoolId = 1;
}
```

```solidity
// contracts/access/Governable.sol:49-53
function __Governable_init() internal onlyInitializing {
    governor = _msgSender();   // caller becomes governor
}
```

### Impact Explanation
`governor` controls `updateMasterOracle` (arbitrary price source → drain pools via mispriced swaps/liquidations), `updateFeeCollector`, `updateSwapper`, `updateOperator`, `registerPool`, `pause`/`shutdown`/`unpause`, and all guardian management. If a frontrun `initialize` succeeds on a proxy that later accumulates user funds (or if the deployer proceeds without noticing), the attacker gains permanent administrative control, enabling theft of user funds via a malicious oracle/swapper or indefinite freezing via `shutdown()`. Realistically, if detected pre-funding, worst case is a re-deploy — consistent with the original finding's Medium severity.

### Likelihood Explanation
Requires an uninitialized proxy to be observable on-chain (or in the mempool) before the legitimate `initialize` executes. The constructors call `_disableInitializers()` on the implementation, so only the proxy context is exploitable, and only in the deployment window. Front-running tools make this straightforward whenever deployment and initialization are not atomic.

### Recommendation
- Deploy proxies with initialization calldata atomically (e.g., `TransparentUpgradeableProxy`/`ERC1967Proxy` constructor `_data`, or a factory that deploys + initializes in one tx).
- Alternatively, have `initialize` accept the intended governor as a parameter or restrict it to a deployer address set in an immutable/constructor-sealed slot.

### Proof of Concept
```solidity
// Foundry-style
function testFrontrunInitialize() public {
    address deployer = address(0xD);
    address attacker = address(0xA);

    // 1. Deployer deploys implementation + proxy, initialize tx is pending in mempool
    PoolRegistry impl = new PoolRegistry();
    vm.prank(deployer);
    TransparentUpgradeableProxy proxy = new TransparentUpgradeableProxy(address(impl), admin, "");

    // 2. Attacker frontruns the initialize call
    vm.prank(attacker);
    PoolRegistry(address(proxy)).initialize(IMasterOracle(attackerOracle), attacker);

    // 3. Attacker is governor; legitimate initialize now reverts
    assertEq(PoolRegistry(address(proxy)).governor(), attacker);
    vm.prank(deployer);
    vm.expectRevert("Initializable: contract is already initialized");
    PoolRegistry(address(proxy)).initialize(IMasterOracle(realOracle), feeCollector);

    // 4. Attacker can now point the master oracle at a malicious contract
    vm.prank(attacker);
    PoolRegistry(address(proxy)).updateMasterOracle(IMasterOracle(maliciousOracle));
}
```

Caveat: I could not verify within the available context whether the production deployment scripts already initialize proxies atomically; if they do, this reduces to a deployment-hygiene hardening item rather than a live vulnerability — mirroring the original finding's downgrade rationale.