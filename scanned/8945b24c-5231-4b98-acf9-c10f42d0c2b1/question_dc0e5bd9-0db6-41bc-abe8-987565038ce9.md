[File: internal/git/gitattributes/check_attr.go -> Scope: Advanced] [Function: CheckAttrCmd.Check path validation] The only sanitization on `path` is rejecting NUL bytes (`strings.Contains(path, \
