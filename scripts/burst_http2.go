// Run: ADMIN_TOKEN=... go run scripts/burst_http2.go -url https://seats.algocrafter.in
// Uses only the Go standard library. HTTP/2 multiplexes requests over TLS connections.
package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/http/httptrace"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

type response struct {
	Status   int
	Data     map[string]any
	Text     string
	Protocol string
	Err      error
	Millis   float64
}
type client struct {
	base        string
	http        *http.Client
	mu          sync.Mutex
	connections map[string]bool
}

func newClient(base string, timeout time.Duration) *client {
	tr := &http.Transport{Proxy: http.ProxyFromEnvironment, ForceAttemptHTTP2: true, MaxConnsPerHost: 256, MaxIdleConns: 256, MaxIdleConnsPerHost: 256, IdleConnTimeout: 2 * time.Second, TLSHandshakeTimeout: 30 * time.Second}
	return &client{base: strings.TrimRight(base, "/"), http: &http.Client{Transport: tr, Timeout: timeout, CheckRedirect: func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }}, connections: map[string]bool{}}
}
func (c *client) call(method, path, token string, body any, requestID string) response {
	encoded, err := json.Marshal(body)
	if err != nil {
		return response{Err: err}
	}
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequest(method, c.base+path, reader)
	if err != nil {
		return response{Err: err}
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if requestID != "" {
		req.Header.Set("X-Request-ID", requestID)
	}
	trace := &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) {
		c.mu.Lock()
		c.connections[info.Conn.LocalAddr().String()+"->"+info.Conn.RemoteAddr().String()] = true
		c.mu.Unlock()
	}}
	req = req.WithContext(httptrace.WithClientTrace(req.Context(), trace))
	start := time.Now()
	res, err := c.http.Do(req)
	if err != nil {
		return response{Err: err, Millis: float64(time.Since(start).Microseconds()) / 1000}
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(res.Body, 2<<20))
	out := response{Status: res.StatusCode, Text: string(raw), Protocol: res.Proto, Err: err, Millis: float64(time.Since(start).Microseconds()) / 1000}
	_ = json.Unmarshal(raw, &out.Data)
	return out
}
func expect(r response, status int) (map[string]any, error) {
	if r.Err != nil {
		return nil, r.Err
	}
	if r.Status != status {
		return nil, fmt.Errorf("expected %d, got %d: %.200s", status, r.Status, r.Text)
	}
	return r.Data, nil
}
func reconciliation(d map[string]any) error {
	counts, ok := d["counts"].(map[string]any)
	if !ok {
		return fmt.Errorf("missing counts")
	}
	total, ok := d["total_seats"].(float64)
	if !ok {
		return fmt.Errorf("missing total")
	}
	actual := map[string]int{"available": 0, "held": 0, "confirmed": 0}
	seen := map[string]bool{}
	seats, ok := d["seats"].([]any)
	if !ok {
		return fmt.Errorf("missing seats")
	}
	for _, raw := range seats {
		s, ok := raw.(map[string]any)
		if !ok {
			return fmt.Errorf("invalid seat")
		}
		key, ok := s["seat_number"].(string)
		if !ok || seen[key] {
			return fmt.Errorf("duplicate/invalid seat")
		}
		seen[key] = true
		state, ok := s["status"].(string)
		if _, known := actual[state]; !ok || !known {
			return fmt.Errorf("invalid seat state")
		}
		actual[state]++
	}
	sum := 0
	for state, n := range actual {
		if counts[state] != float64(n) {
			return fmt.Errorf("seat counts differ")
		}
		sum += n
	}
	if float64(sum) != total {
		return fmt.Errorf("reconciliation failed")
	}
	return nil
}
func percentile(v []float64, p float64) float64 {
	if len(v) == 0 {
		return 0
	}
	return v[int(float64(len(v)-1)*p)]
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run() error {
	url := flag.String("url", "https://seats.algocrafter.in", "Public API URL")
	requests := flag.Int("requests", 20000, "Reservation requests")
	concurrency := flag.Int("concurrency", 20000, "Concurrent client tasks")
	users := flag.Int("users", 500, "Distinct authenticated users")
	seconds := flag.Int("timeout", 240, "Timeout per HTTP request in seconds")
	flag.Parse()
	admin := os.Getenv("ADMIN_TOKEN")
	if admin == "" || *requests < 16 || *concurrency < 1 || *users < 2 || *seconds < 1 {
		return fmt.Errorf("set ADMIN_TOKEN; requests >=16, concurrency >=1, users >=2, timeout >=1")
	}
	control := newClient(*url, time.Duration(*seconds)*time.Second)
	load := newClient(*url, time.Duration(*seconds)*time.Second)
	entropy := make([]byte, 8)
	if _, err := rand.Read(entropy); err != nil {
		return err
	}
	prefix := "h2-" + hex.EncodeToString(entropy)
	if _, err := expect(control.call("GET", "/health/ready", "", nil, ""), 200); err != nil {
		return err
	}
	// Warm the transport so HTTP/2 is negotiated before opening the burst.
	warm := load.call("GET", "/health/ready", "", nil, "")
	if _, err := expect(warm, 200); err != nil {
		return err
	}
	tokens := make([]string, *users)
	var wg sync.WaitGroup
	gate := make(chan struct{}, 20)
	errs := make(chan error, *users)
	for i := range tokens {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			gate <- struct{}{}
			defer func() { <-gate }()
			d, err := expect(control.call("POST", "/auth/token", admin, map[string]any{"user_id": fmt.Sprintf("%s-user-%d", prefix, i)}, ""), 200)
			if err != nil {
				errs <- err
				return
			}
			tokens[i], _ = d["access_token"].(string)
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		return err
	}
	create := func(label string, seats []string, limit int) (map[string]any, error) {
		return expect(control.call("POST", "/shows", admin, map[string]any{"name": prefix + "-" + label, "seats": seats, "price_paise": 25000, "per_user_limit": limit}, ""), 201)
	}
	reserve := func(c *client, id string, seats []string, key string, user int, rid string) response {
		return c.call("POST", "/shows/"+id+"/reserve", tokens[user], map[string]any{"seats": seats, "idempotency_key": key}, rid)
	}
	seats := []string{"HOT1", "HOT2", "HOT3", "HOT4", "HOT5", "HOT6", "HOT7", "HOT8", "REPLAY", "UNSOLD"}
	show, err := create("storm", seats, 9)
	if err != nil {
		return err
	}
	id := show["id"].(string)
	original, err := expect(reserve(control, id, []string{"REPLAY"}, "original", 0, ""), 201)
	if err != nil {
		return err
	}
	type result struct {
		response response
		seat     string
		replay   bool
	}
	results := make(chan result, *requests)
	sampleDone := make(chan struct{})
	samplingStopped := make(chan struct{})
	snapshots := 0
	sampleErrors := []string{}
	go func() {
		defer close(samplingStopped)
		for {
			select {
			case <-sampleDone:
				return
			default:
			}
			d, e := expect(control.call("GET", "/shows/"+id, "", nil, ""), 200)
			if e == nil {
				e = reconciliation(d)
			}
			if e != nil {
				sampleErrors = append(sampleErrors, e.Error())
			} else {
				snapshots++
			}
			select {
			case <-sampleDone:
				return
			case <-time.After(250 * time.Millisecond):
			}
		}
	}()
	startGate := make(chan struct{})
	work := make(chan struct{}, *concurrency)
	fmt.Printf("Sending %d requests, %d client tasks; negotiated %s; show %s\n", *requests, *concurrency, warm.Protocol, id)
	started := time.Now()
	for i := 0; i < *requests; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-startGate
			work <- struct{}{}
			defer func() { <-work }()
			seat := seats[i%8]
			key := fmt.Sprintf("%s-%d", prefix, i)
			user := i % *users
			replay := i >= 8 && i%11 == 0
			if replay {
				seat = "REPLAY"
				key = "original"
				user = 0
			}
			results <- result{reserve(load, id, []string{seat}, key, user, fmt.Sprintf("%s-%d", prefix, i)), seat, replay}
		}(i)
	}
	close(startGate)
	wg.Wait()
	elapsed := time.Since(started).Seconds()
	close(results)
	close(sampleDone)
	<-samplingStopped
	statuses := map[int]int{}
	protocols := map[string]int{}
	outcomes := map[string]int{"confirmed": 0, "seat_taken": 0, "idempotent_replay": 0, "5xx": 0, "transport_failure": 0}
	winners := map[string]int{}
	failures := sampleErrors
	latencies := []float64{}
	for item := range results {
		r := item.response
		if r.Err != nil {
			outcomes["transport_failure"]++
			failures = append(failures, r.Err.Error())
			continue
		}
		statuses[r.Status]++
		protocols[r.Protocol]++
		latencies = append(latencies, r.Millis)
		switch {
		case r.Status == 201:
			outcomes["confirmed"]++
			winners[item.seat]++
			if r.Data["amount_paise"] != float64(25000) {
				failures = append(failures, "incorrect price")
			}
		case r.Status == 200:
			outcomes["idempotent_replay"]++
			if !item.replay || r.Data["reservation_id"] != original["reservation_id"] {
				failures = append(failures, "wrong replay")
			}
		case r.Status == 409:
			e, _ := r.Data["error"].(map[string]any)
			reason, _ := e["code"].(string)
			outcomes[reason]++
			if reason != "seat_taken" {
				failures = append(failures, "unexpected decline: "+reason)
			}
		case r.Status >= 500:
			outcomes["5xx"]++
			failures = append(failures, fmt.Sprintf("HTTP %d: %.200s", r.Status, r.Text))
		default:
			failures = append(failures, fmt.Sprintf("unexpected HTTP %d", r.Status))
		}
	}
	for _, seat := range seats[:8] {
		if winners[seat] != 1 {
			failures = append(failures, fmt.Sprintf("%s winners=%d", seat, winners[seat]))
		}
	}
	state, err := expect(control.call("GET", "/shows/"+id, "", nil, ""), 200)
	if err == nil {
		err = reconciliation(state)
	}
	if err != nil {
		failures = append(failures, err.Error())
	}
	metrics := control.call("GET", "/metrics", "", nil, "")
	metricsMatch := metrics.Status == 200
	if counts, ok := state["counts"].(map[string]any); ok {
		for _, s := range []string{"available", "held", "confirmed"} {
			needle := fmt.Sprintf("seats_%s{show_id=\"%s\"} %g", s, id, counts[s])
			found := false
			for _, line := range strings.Split(metrics.Text, "\n") {
				if line == needle || line == needle+".0" {
					found = true
				}
			}
			metricsMatch = metricsMatch && found
		}
	} else {
		metricsMatch = false
	}
	if !metricsMatch {
		failures = append(failures, "metrics mismatch")
	}
	scenarios := map[string]bool{}
	check := func(name string, r response, status int) {
		_, e := expect(r, status)
		scenarios[name] = e == nil
		if e != nil {
			failures = append(failures, name+": "+e.Error())
		}
	}
	check("same_key_different_body", reserve(control, id, []string{"UNSOLD"}, "original", 0, ""), 409)
	qSeats := []string{"Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10"}
	quota, e := create("quota", qSeats, 4)
	if e != nil {
		failures = append(failures, e.Error())
	} else {
		qid := quota["id"].(string)
		qr := make(chan response, 10)
		for i, s := range qSeats {
			wg.Add(1)
			go func(i int, s string) {
				defer wg.Done()
				qr <- reserve(control, qid, []string{s}, fmt.Sprintf("quota-%d", i), 0, "")
			}(i, s)
		}
		wg.Wait()
		close(qr)
		won := 0
		valid := true
		for r := range qr {
			if r.Status == 201 {
				won++
			} else if r.Status != 409 {
				valid = false
			}
		}
		scenarios["parallel_user_limit"] = won == 4 && valid
		if !scenarios["parallel_user_limit"] {
			failures = append(failures, "parallel quota failed")
		}
	}
	extra, e := create("cancellation", []string{"A1", "A2"}, 4)
	if e != nil {
		failures = append(failures, e.Error())
	} else {
		eid := extra["id"].(string)
		first, e := expect(reserve(control, eid, []string{"A1"}, "first", 0, ""), 201)
		if e != nil {
			failures = append(failures, e.Error())
		} else {
			cancelPath := "/reservations/" + first["reservation_id"].(string) + "/cancel"
			check("all_or_nothing", reserve(control, eid, []string{"A1", "A2"}, "partial", 1, ""), 409)
			check("nonowner_cancel", control.call("POST", cancelPath, tokens[1], nil, ""), 403)
			check("owner_cancel", control.call("POST", cancelPath, tokens[0], nil, ""), 200)
			check("rebook", reserve(control, eid, []string{"A1", "A2"}, "rebook", 1, ""), 201)
			check("stale_cancel", control.call("POST", cancelPath, tokens[0], nil, ""), 200)
			final, e := expect(control.call("GET", "/shows/"+eid, "", nil, ""), 200)
			if e == nil {
				e = reconciliation(final)
			}
			if e != nil {
				failures = append(failures, e.Error())
			} else if final["counts"].(map[string]any)["confirmed"] != float64(2) {
				failures = append(failures, "stale cancellation released rebooked seats")
			}
			check("spoofed_identity", control.call("POST", "/shows/"+eid+"/reserve", tokens[0], map[string]any{"seats": []string{"A2"}, "idempotency_key": "spoof", "user_id": "victim"}, ""), 422)
		}
	}
	sort.Float64s(latencies)
	load.mu.Lock()
	connections := len(load.connections)
	load.mu.Unlock()
	firstErrors := failures
	if len(firstErrors) > 20 {
		firstErrors = firstErrors[:20]
	}
	report := map[string]any{"passed": len(failures) == 0, "requests": *requests, "concurrency": *concurrency, "users": *users, "seconds": elapsed, "show_id": id, "http_statuses": statuses, "http_protocols": protocols, "connections_used": connections, "outcomes": outcomes, "hot_seat_201_counts": winners, "invariant_snapshots_during_burst": snapshots, "final_reconciliation": state["counts"], "metrics_match_final_state": metricsMatch, "additional_scenarios": scenarios, "http_response_latency_ms": map[string]float64{"p50": percentile(latencies, .5), "p95": percentile(latencies, .95), "p99": percentile(latencies, .99)}, "error_count": len(failures), "errors_first_20": firstErrors}
	encoded, _ := json.MarshalIndent(report, "", "  ")
	fmt.Println(string(encoded))
	if len(failures) > 0 {
		return fmt.Errorf("burst failed: %d errors", len(failures))
	}
	return nil
}
