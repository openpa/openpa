// Package wsclient is a minimal RFC 6455 WebSocket client tailored to the
// OpenPA CLI's needs:
//
//   - Text frames only (the server sends and accepts JSON payloads).
//   - Bearer auth via the Sec-WebSocket-Protocol subprotocol header
//     ("bearer", <token>) — matching what openpa-ui does because browsers
//     can't set Authorization on the native WebSocket constructor.
//   - Synchronous WriteText / ReadText API. Concurrent writes are serialized
//     by an internal mutex; reads are single-goroutine by convention.
//   - Standard library only — no external WS library to avoid pulling deps
//     for one feature.
//
// Limitations: no permessage-deflate, no fragmented frames on send, and only
// rudimentary control-frame handling (ping → pong reply, close → return EOF).
// Server frames may be fragmented; the reader stitches them back together.
package wsclient

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Conn is an open WebSocket. It is safe to call WriteText concurrently from
// multiple goroutines. ReadText is intended to be called from a single
// goroutine.
type Conn struct {
	conn   net.Conn
	br     *bufio.Reader
	wmu    sync.Mutex
	closed bool
}

// Dial opens a WebSocket connection to the given ws:// or wss:// URL. token
// is sent via the Sec-WebSocket-Protocol header as ("bearer", token); pass
// "" to skip subprotocol negotiation. The dial respects ctx for both the TCP
// connect and the HTTP upgrade exchange.
func Dial(ctx context.Context, rawURL, token string) (*Conn, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("parse ws url: %w", err)
	}

	host := u.Host
	var dialer net.Dialer
	switch u.Scheme {
	case "ws":
		if !strings.Contains(host, ":") {
			host += ":80"
		}
		conn, err := dialer.DialContext(ctx, "tcp", host)
		if err != nil {
			return nil, err
		}
		return finishHandshake(ctx, conn, u, token)
	case "wss":
		if !strings.Contains(host, ":") {
			host += ":443"
		}
		conn, err := tlsDial(ctx, host, u.Hostname())
		if err != nil {
			return nil, err
		}
		return finishHandshake(ctx, conn, u, token)
	default:
		return nil, fmt.Errorf("unsupported scheme %q (need ws:// or wss://)", u.Scheme)
	}
}

func finishHandshake(ctx context.Context, conn net.Conn, u *url.URL, token string) (*Conn, error) {
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}

	keyBytes := make([]byte, 16)
	if _, err := rand.Read(keyBytes); err != nil {
		_ = conn.Close()
		return nil, err
	}
	key := base64.StdEncoding.EncodeToString(keyBytes)

	path := u.RequestURI()
	if path == "" {
		path = "/"
	}

	req := strings.Builder{}
	req.WriteString("GET " + path + " HTTP/1.1\r\n")
	req.WriteString("Host: " + u.Host + "\r\n")
	req.WriteString("Upgrade: websocket\r\n")
	req.WriteString("Connection: Upgrade\r\n")
	req.WriteString("Sec-WebSocket-Version: 13\r\n")
	req.WriteString("Sec-WebSocket-Key: " + key + "\r\n")
	if token != "" {
		req.WriteString("Sec-WebSocket-Protocol: bearer, " + token + "\r\n")
	}
	req.WriteString("\r\n")

	if _, err := io.WriteString(conn, req.String()); err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("write upgrade request: %w", err)
	}

	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("read upgrade response: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusSwitchingProtocols {
		_ = conn.Close()
		body, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("websocket upgrade failed: %s — %s",
			resp.Status, strings.TrimSpace(string(body)))
	}
	if !strings.EqualFold(resp.Header.Get("Upgrade"), "websocket") ||
		!headerContainsToken(resp.Header.Get("Connection"), "upgrade") {
		_ = conn.Close()
		return nil, fmt.Errorf("server did not honor upgrade headers")
	}
	if resp.Header.Get("Sec-WebSocket-Accept") != expectedAccept(key) {
		_ = conn.Close()
		return nil, fmt.Errorf("invalid Sec-WebSocket-Accept")
	}

	// Clear deadlines now that the handshake is done; subsequent I/O uses
	// per-call deadlines via the caller's context.
	_ = conn.SetDeadline(time.Time{})

	return &Conn{conn: conn, br: br}, nil
}

func headerContainsToken(header, want string) bool {
	want = strings.ToLower(want)
	for _, part := range strings.Split(header, ",") {
		if strings.EqualFold(strings.TrimSpace(part), want) {
			return true
		}
	}
	return false
}

const wsGUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

func expectedAccept(key string) string {
	h := sha1.New()
	h.Write([]byte(key))
	h.Write([]byte(wsGUID))
	return base64.StdEncoding.EncodeToString(h.Sum(nil))
}

// Close gracefully closes the connection by sending a close frame, then the
// underlying TCP connection.
func (c *Conn) Close() error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if c.closed {
		return nil
	}
	c.closed = true
	// 1000 (normal closure), no reason.
	payload := []byte{0x03, 0xe8}
	_ = c.writeFrameLocked(0x8 /*Close*/, payload)
	return c.conn.Close()
}

// WriteText sends a single (unfragmented) text frame containing s.
func (c *Conn) WriteText(s string) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()
	if c.closed {
		return errors.New("websocket closed")
	}
	return c.writeFrameLocked(0x1 /*Text*/, []byte(s))
}

// ReadText returns the payload of the next text frame received. Ping frames
// are answered with pongs and skipped. Close frames return io.EOF.
func (c *Conn) ReadText() (string, error) {
	for {
		opcode, payload, err := c.readFrame()
		if err != nil {
			return "", err
		}
		switch opcode {
		case 0x1 /*Text*/, 0x2 /*Binary*/ :
			return string(payload), nil
		case 0x8 /*Close*/ :
			return "", io.EOF
		case 0x9 /*Ping*/ :
			c.wmu.Lock()
			err := c.writeFrameLocked(0xA /*Pong*/, payload)
			c.wmu.Unlock()
			if err != nil {
				return "", err
			}
		case 0xA /*Pong*/ :
			// Ignore unsolicited pongs.
		default:
			// Unknown opcode — ignore and keep reading.
		}
	}
}

// readFrame returns the opcode and assembled payload of the next *application*
// data unit. Fragmented frames (FIN=0 followed by continuation frames) are
// stitched together transparently. Control frames (Close/Ping/Pong) are
// returned individually.
func (c *Conn) readFrame() (byte, []byte, error) {
	var first byte
	var payload []byte
	for {
		hdr := [2]byte{}
		if _, err := io.ReadFull(c.br, hdr[:]); err != nil {
			return 0, nil, err
		}
		fin := hdr[0]&0x80 != 0
		opcode := hdr[0] & 0x0f
		masked := hdr[1]&0x80 != 0
		length := int(hdr[1] & 0x7f)

		switch length {
		case 126:
			var ext [2]byte
			if _, err := io.ReadFull(c.br, ext[:]); err != nil {
				return 0, nil, err
			}
			length = int(binary.BigEndian.Uint16(ext[:]))
		case 127:
			var ext [8]byte
			if _, err := io.ReadFull(c.br, ext[:]); err != nil {
				return 0, nil, err
			}
			n := binary.BigEndian.Uint64(ext[:])
			if n > (1<<31)-1 {
				return 0, nil, errors.New("ws frame too large")
			}
			length = int(n)
		}

		var maskKey [4]byte
		if masked {
			if _, err := io.ReadFull(c.br, maskKey[:]); err != nil {
				return 0, nil, err
			}
		}
		buf := make([]byte, length)
		if length > 0 {
			if _, err := io.ReadFull(c.br, buf); err != nil {
				return 0, nil, err
			}
			if masked {
				for i := range buf {
					buf[i] ^= maskKey[i%4]
				}
			}
		}

		// Control frames (opcode >= 0x8) are never fragmented per RFC.
		if opcode >= 0x8 {
			return opcode, buf, nil
		}
		if opcode != 0 {
			first = opcode
			payload = buf
		} else {
			payload = append(payload, buf...)
		}
		if fin {
			return first, payload, nil
		}
	}
}

// writeFrameLocked writes a single frame. Caller MUST hold c.wmu.
func (c *Conn) writeFrameLocked(opcode byte, payload []byte) error {
	hdr := []byte{0x80 | opcode} // FIN=1, opcode
	plen := len(payload)
	switch {
	case plen <= 125:
		hdr = append(hdr, byte(plen)|0x80)
	case plen <= 65535:
		hdr = append(hdr, 126|0x80)
		var ext [2]byte
		binary.BigEndian.PutUint16(ext[:], uint16(plen))
		hdr = append(hdr, ext[:]...)
	default:
		hdr = append(hdr, 127|0x80)
		var ext [8]byte
		binary.BigEndian.PutUint64(ext[:], uint64(plen))
		hdr = append(hdr, ext[:]...)
	}
	var mask [4]byte
	if _, err := rand.Read(mask[:]); err != nil {
		return err
	}
	hdr = append(hdr, mask[:]...)
	masked := make([]byte, plen)
	for i, b := range payload {
		masked[i] = b ^ mask[i%4]
	}
	if _, err := c.conn.Write(hdr); err != nil {
		return err
	}
	if plen > 0 {
		if _, err := c.conn.Write(masked); err != nil {
			return err
		}
	}
	return nil
}
