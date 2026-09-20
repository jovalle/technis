package main

import (
	"bytes"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestParseLogLevel(t *testing.T) {
	tests := map[string]slog.Level{
		"":        slog.LevelInfo,
		"debug":   slog.LevelDebug,
		"info":    slog.LevelInfo,
		"warn":    slog.LevelWarn,
		"warning": slog.LevelWarn,
		"error":   slog.LevelError,
	}
	for value, expected := range tests {
		actual, err := parseLogLevel(value)
		if err != nil {
			t.Fatalf("parseLogLevel(%q): %v", value, err)
		}
		if actual != expected {
			t.Errorf("parseLogLevel(%q) = %v, want %v", value, actual, expected)
		}
	}
	if _, err := parseLogLevel("trace"); err == nil {
		t.Error("parseLogLevel(\"trace\") returned no error")
	}
}

func TestHeartbeatLogging(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	var output bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&output, &slog.HandlerOptions{Level: slog.LevelDebug}))
	client := &http.Client{Transport: &debugTransport{base: http.DefaultTransport, logger: logger}}
	runHeartbeat(client, logger, server.URL+"/secret-id", "example.test", 1)

	logs := output.String()
	for _, expected := range []string{"heartbeat attempt", "HTTP request", server.URL + "/secret-id", "HTTP response", "heartbeat succeeded"} {
		if !strings.Contains(logs, expected) {
			t.Errorf("logs do not contain %q:\n%s", expected, logs)
		}
	}
}

func TestHeartbeatWarnsOnNon2xx(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	var output bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&output, &slog.HandlerOptions{Level: slog.LevelInfo}))
	runHeartbeat(server.Client(), logger, server.URL, "example.test", 1)

	logs := output.String()
	if !strings.Contains(logs, `"level":"WARN"`) || !strings.Contains(logs, "heartbeat returned non-2xx status") {
		t.Errorf("logs do not contain a non-2xx warning:\n%s", logs)
	}
}
