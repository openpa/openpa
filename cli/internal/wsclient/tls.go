package wsclient

import (
	"context"
	"crypto/tls"
	"net"
)

// tlsDial dials addr ("host:port") with TLS, using sni as the SNI hostname
// and verification target.
func tlsDial(ctx context.Context, addr, sni string) (net.Conn, error) {
	var dialer net.Dialer
	raw, err := dialer.DialContext(ctx, "tcp", addr)
	if err != nil {
		return nil, err
	}
	tc := tls.Client(raw, &tls.Config{ServerName: sni})
	if err := tc.HandshakeContext(ctx); err != nil {
		_ = raw.Close()
		return nil, err
	}
	return tc, nil
}
