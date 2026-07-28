# Q3880: Smart account deployment and initialization: init arrays / address confusion / ownership binding

## Question
Can an unprivileged deployer or external caller with no guardian or admin powers enter through `SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)` with delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer while other users or systems rely on the predicted address before the account is actually deployed and make predicted-address semantics diverge from the actual deployed account or from who effectively controls it, breaking the rule that deployment should never create a Safe whose effective post-init control diverges from the declared owners and threshold and leading to Theft or redirection of value sent to a predicted or freshly deployed smart account?

## Target
- File/function: contracts/SmartAccountFactory.sol / deploySmartAccount, configureSmartAccount, predictSmartAccountAddress
- Entrypoint: SmartAccountFactory.deploySmartAccount(...) and predictSmartAccountAddress(...)
- Attacker controls: delegates, currencies, NFT collections, trusted recipients, owners, and threshold in the deployment initializer
- Exploit idea: make predicted-address semantics diverge from the actual deployed account or from who effectively controls it
- Invariant to test: deployment should never create a Safe whose effective post-init control diverges from the declared owners and threshold
- Expected Immunefi impact: Theft or redirection of value sent to a predicted or freshly deployed smart account
- Fast validation: Model predicted-address observation and deployment races and ensure a third party cannot change who effectively controls the resulting account.
