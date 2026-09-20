package main

import (
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
)

const checkInterval = 10 * time.Second

func main() {
	targetURL := os.Getenv("GO_CHECK_URL")
	if targetURL == "" {
		fmt.Fprintln(os.Stderr, "GO_CHECK_URL is required")
		os.Exit(1)
	}
	parsedURL, err := url.ParseRequestURI(targetURL)
	if err != nil || parsedURL.Scheme == "" || parsedURL.Host == "" {
		fmt.Fprintln(os.Stderr, "GO_CHECK_URL must be an absolute URL")
		os.Exit(1)
	}

	logLevel, err := parseLogLevel(os.Getenv("GO_CHECK_LOG_LEVEL"))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: logLevel}))

	client := &http.Client{
		Timeout:   10 * time.Second,
		Transport: &debugTransport{base: http.DefaultTransport, logger: logger},
	}

	logger.Info("go-check started", "host", parsedURL.Host, "interval", checkInterval.String())
	for attempt := uint64(1); ; attempt++ {
		runHeartbeat(client, logger, targetURL, parsedURL.Host, attempt)
		time.Sleep(checkInterval)
	}
}

func parseLogLevel(value string) (slog.Level, error) {
	switch strings.ToLower(value) {
	case "", "info":
		return slog.LevelInfo, nil
	case "debug":
		return slog.LevelDebug, nil
	case "warn", "warning":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return 0, fmt.Errorf("invalid GO_CHECK_LOG_LEVEL %q: use debug, info, warn, or error", value)
	}
}

func runHeartbeat(client *http.Client, logger *slog.Logger, targetURL, host string, attempt uint64) {
	logger.Info("heartbeat attempt", "attempt", attempt, "method", http.MethodHead, "host", host)
	started := time.Now()
	response, err := client.Head(targetURL)
	duration := time.Since(started)
	if err != nil {
		logger.Error("heartbeat failed", "attempt", attempt, "duration", duration.String(), "error", err)
		return
	}
	response.Body.Close()

	attributes := []any{"attempt", attempt, "status", response.StatusCode, "duration", duration.String()}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		logger.Warn("heartbeat returned non-2xx status", attributes...)
		return
	}
	logger.Info("heartbeat succeeded", attributes...)
}

type debugTransport struct {
	base   http.RoundTripper
	logger *slog.Logger
}

func (transport *debugTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	debugEnabled := transport.logger.Enabled(request.Context(), slog.LevelDebug)
	if debugEnabled {
		if dump, err := httputil.DumpRequestOut(request, false); err != nil {
			transport.logger.Debug("could not dump HTTP request", "url", request.URL.String(), "error", err)
		} else {
			transport.logger.Debug("HTTP request", "url", request.URL.String(), "raw", string(dump))
		}
	}

	response, err := transport.base.RoundTrip(request)
	if err != nil {
		return nil, err
	}
	if debugEnabled {
		if dump, err := httputil.DumpResponse(response, false); err != nil {
			transport.logger.Debug("could not dump HTTP response", "url", request.URL.String(), "error", err)
		} else {
			transport.logger.Debug("HTTP response", "url", request.URL.String(), "raw", string(dump))
		}
	}
	return response, nil
}

var _ http.RoundTripper = (*debugTransport)(nil)
