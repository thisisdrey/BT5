No vulnerability found for this question.

The Samba CVE-2020-27840 concerns parsing of LDAP-style distinguished-name (DN) *strings* (with embedded/surrounding spaces) into an internal DN representation, where malformed spacing causes an out-of-bounds write during string-to-structure parsing.

In this Go repository, the closest related code is `crypto/x509/pkix`, but its `RDNSequence` handling only goes in the opposite direction: `parseName` in `crypto/x509/parser.go` decodes DER-encoded ASN.1 `Name` structures (not free-form DN strings) [1](#0-0) , and `RDNSequence.String()` / `Name.String()` in `crypto/x509/pkix/pkix.go` only *serialize* an already-parsed RDN sequence into an RFC 2253-style string, escaping characters like leading/trailing spaces — it never parses an attacker-supplied DN string back into a structure [2](#0-1) [3](#0-2) . There is no Go standard-library equivalent of Samba's LDAP DN string parser (`ldb`), so there is no reachable "space in DN string → out-of-bounds write" primitive in this codebase. A search for `ParseDN`/`UnmarshalDN`-style functions found no matching string-to-DN parser in the standard library (only unrelated matches in `net/dnsclient_unix.go` and `text/template/parse/parse.go`) [4](#0-3) .

Since the vulnerable primitive (parsing a raw, space-padded DN string into memory) does not exist in this repository's production code, no valid analog can be constructed.

### Citations

**File:** src/crypto/x509/parser.go (L207-242)
```go
// parseName parses a DER encoded Name as defined in RFC 5280. We may
// want to export this function in the future for use in crypto/tls.
func parseName(raw cryptobyte.String) (*pkix.RDNSequence, error) {
	if !raw.ReadASN1(&raw, cryptobyte_asn1.SEQUENCE) {
		return nil, errors.New("x509: invalid RDNSequence")
	}

	var rdnSeq pkix.RDNSequence
	for !raw.Empty() {
		var rdnSet pkix.RelativeDistinguishedNameSET
		var set cryptobyte.String
		if !raw.ReadASN1(&set, cryptobyte_asn1.SET) {
			return nil, errors.New("x509: invalid RDNSequence")
		}
		for !set.Empty() {
			var atav cryptobyte.String
			if !set.ReadASN1(&atav, cryptobyte_asn1.SEQUENCE) {
				return nil, errors.New("x509: invalid RDNSequence: invalid attribute")
			}
			var attr pkix.AttributeTypeAndValue
			if !atav.ReadASN1ObjectIdentifier(&attr.Type) {
				return nil, errors.New("x509: invalid RDNSequence: invalid attribute type")
			}
			var err error
			attr.Value, err = readASN1Any(&atav)
			if err != nil {
				return nil, fmt.Errorf("x509: invalid RDNSequence: invalid attribute value: %s", err)
			}
			rdnSet = append(rdnSet, attr)
		}

		rdnSeq = append(rdnSeq, rdnSet)
	}

	return &rdnSeq, nil
}
```

**File:** src/crypto/x509/pkix/pkix.go (L39-103)
```go
// String returns a string representation of the sequence r,
// roughly following the RFC 2253 Distinguished Names syntax.
func (r RDNSequence) String() string {
	var buf strings.Builder
	for i := 0; i < len(r); i++ {
		rdn := r[len(r)-1-i]
		if i > 0 {
			buf.WriteByte(',')
		}
		for j, tv := range rdn {
			if j > 0 {
				buf.WriteByte('+')
			}

			oidString := tv.Type.String()
			typeName, ok := attributeTypeNames[oidString]
			if !ok {
				// RFC 2253 §2.4: if the value's ASN.1 type has a string
				// representation, render it as a string; otherwise hex-encode
				// the DER.
				if _, ok := tv.Value.(string); !ok {
					derBytes, err := asn1.Marshal(tv.Value)
					if err == nil {
						buf.WriteString(oidString)
						buf.WriteString("=#")
						buf.WriteString(hex.EncodeToString(derBytes))
						continue // No value escaping necessary.
					}
				}

				typeName = oidString
			}

			valueString := fmt.Sprint(tv.Value)
			escaped := make([]rune, 0, len(valueString))

			for k, c := range valueString {
				escape := false

				switch c {
				case ',', '+', '"', '\\', '<', '>', ';':
					escape = true

				case ' ':
					escape = k == 0 || k == len(valueString)-1

				case '#':
					escape = k == 0
				}

				if escape {
					escaped = append(escaped, '\\', c)
				} else {
					escaped = append(escaped, c)
				}
			}

			buf.WriteString(typeName)
			buf.WriteByte('=')
			buf.WriteString(string(escaped))
		}
	}

	return buf.String()
}
```

**File:** src/crypto/x509/pkix/pkix.go (L271-294)
```go
// String returns the string form of n, roughly following
// the RFC 2253 Distinguished Names syntax.
func (n Name) String() string {
	var rdns RDNSequence
	// If there are no ExtraNames, surface the parsed value (all entries in
	// Names) instead.
	if n.ExtraNames == nil {
		for _, atv := range n.Names {
			t := atv.Type
			if len(t) == 4 && t[0] == 2 && t[1] == 5 && t[2] == 4 {
				switch t[3] {
				case 3, 5, 6, 7, 8, 9, 10, 11, 17:
					// These attributes were already parsed into named fields.
					continue
				}
			}
			// Place non-standard parsed values at the beginning of the sequence
			// so they will be at the end of the string. See Issue 39924.
			rdns = append(rdns, []AttributeTypeAndValue{atv})
		}
	}
	rdns = append(rdns, n.ToRDNSequence()...)
	return rdns.String()
}
```

**File:** src/crypto/x509/parser_test.go (L1-1)
```go
// Copyright 2021 The Go Authors. All rights reserved.
```
